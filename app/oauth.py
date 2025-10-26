# app/oauth.py
import time
import logging
import requests
from aiohttp import web
from app.utils.messages import (setup_success, viewer_is_owner, viewer_is_sub, viewer_not_sub)

class OAuthHandlers:
    def __init__(self, cfg, db, send_tg, refresh_twitch_token, days_until, cipher):
        self.cfg = cfg
        self.db = db
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token
        self.days_until = days_until
        self.cipher = cipher

    # ───────────────────────────────────────────────
    # CALLBACK SETUP (STREAMER)
    # ───────────────────────────────────────────────
    async def twitch_callback_setup(self, request: web.Request):
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta code/state.", status=400)

            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='broadcaster_setup'",
                state
            )
            if not row:
                return web.Response(text="❌ Estado inválido o caducado.", status=400)

            # Intercambio del código por tokens
            data = requests.post(self.cfg.TW_TOKEN, data={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{self.cfg.base_url}/twitch/setup/callback"
            }, timeout=15).json()

            if "access_token" not in data:
                logging.error(f"❌ Error al obtener token: {data}")
                return web.Response(text="❌ No se pudo obtener el token de Twitch.", status=400)

            access_token = data["access_token"]
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 0)

            # Obtener info del canal
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": self.cfg.client_id,
            }
            me = requests.get(f"{self.cfg.TW_API}/users", headers=headers, timeout=15).json()["data"][0]
            slug = me["login"].lower()
            avatar = me.get("profile_image_url")

            # Cifrar tokens
            enc_access = self.cipher.encrypt(access_token)
            enc_refresh = self.cipher.encrypt(refresh_token)

            # Crear nuevo token para dashboard
            import secrets
            dashboard_token = secrets.token_urlsafe(32)

            # Guardar broadcaster
            await self.db.exec(
                """
                INSERT INTO broadcasters (
                    broadcaster_id, display_name, slug, access_token, refresh_token,
                    token_obtained_at, token_expires_in, owner_telegram_id, dashboard_token, profile_image_url
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (broadcaster_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    slug=EXCLUDED.slug,
                    access_token=EXCLUDED.access_token,
                    refresh_token=EXCLUDED.refresh_token,
                    token_obtained_at=EXCLUDED.token_obtained_at,
                    token_expires_in=EXCLUDED.token_expires_in,
                    owner_telegram_id=EXCLUDED.owner_telegram_id,
                    dashboard_token=EXCLUDED.dashboard_token,
                    profile_image_url=EXCLUDED.profile_image_url
                """,
                me["id"], me["display_name"], slug,
                enc_access, enc_refresh, int(time.time()), expires_in,
                row["telegram_id"], dashboard_token, avatar
            )

            dash_link = f"{self.cfg.base_url}/dashboard/{slug}?token={dashboard_token}"

            # Mensaje principal con botones inline (Dashboard + Guía)
            msg, keyboard = setup_success(me["display_name"], self.cfg.bot_username, slug, dash_link)

            # 👇 Enviar mensaje sin previsualización del enlace
            await self.send_tg(
                row["telegram_id"],
                msg,
                html=True,
                disable_preview=True,
                reply_markup=keyboard
            )

            return web.Response(
                text=f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                     "<h3>✅ Vinculación completada. Puedes volver a Telegram.</h3>",
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_setup:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)

        # ───────────────────────────────────────────────
    # CALLBACK USUARIO / VIEWER
    # ───────────────────────────────────────────────
    async def twitch_callback_user(self, request: web.Request):
        import secrets
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta parámetros.", status=400)

            # Buscar estado OAuth válido
            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='user_link'",
                state
            )
            if not row:
                return web.Response(text="❌ Estado inválido o caducado.", status=400)

            # Intercambio del código por token
            data = requests.post(self.cfg.TW_TOKEN, data={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.cfg.base_url}/twitch/callback"
            }, timeout=15).json()

            if "access_token" not in data:
                return web.Response(text=f"Error al obtener token: {data}", status=400)

            access_token = data["access_token"]
            headers_user = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": self.cfg.client_id
            }

            # Obtener datos del usuario de Twitch
            me = requests.get(f"{self.cfg.TW_API}/users", headers=headers_user, timeout=15).json()["data"][0]

            # Guardar usuario en telegram_users
            await self.db.exec("""
                INSERT INTO group_members (
                    group_id, telegram_id, username, twitch_login,
                    broadcaster_id, streamer_slug, joined_at, is_subscribed
                ) VALUES (%s,%s,%s,%s,%s,%s,NOW(),%s)
                ON CONFLICT (group_id, telegram_id)
                DO UPDATE SET
                    username=EXCLUDED.username,
                    twitch_login=EXCLUDED.twitch_login,
                    is_subscribed=EXCLUDED.is_subscribed,
                    joined_at=NOW();
            """,
                b["group_id"], row["telegram_id"], me["login"], me["id"],  # 👈 ID real de Twitch aquí
                b["broadcaster_id"], b["slug"], is_sub
            )

            # Obtener broadcaster asociado
            b = await self.db.one("SELECT * FROM broadcasters WHERE broadcaster_id=%s", row["broadcaster_id"])
            if not b:
                return web.Response(text="Streamer no encontrado.", status=400)

            # Refrescar token si ha caducado
            b_access = await self.refresh_twitch_token(b)
            headers_b = {"Authorization": f"Bearer {b_access}", "Client-Id": self.cfg.client_id}

            # Verificar si el usuario es sub
            r = requests.get(
                f"{self.cfg.TW_API}/subscriptions/user",
                headers=headers_user,
                params={"broadcaster_id": b["broadcaster_id"], "user_id": me["id"]},
                timeout=15
            )
            res_json = r.json()
            data_sub = res_json.get("data", [])
            logging.info(f"Twitch API subscriptions response: {res_json}")

            # ───────────────────────────────
            # Detección del tipo de usuario
            # ───────────────────────────────
            if me["id"] == b["broadcaster_id"]:
                msg, keyboard = viewer_is_owner(me["display_name"], b["display_name"], b.get("invite_link"))
                is_sub = True
            elif data_sub:
                msg, keyboard = viewer_is_sub(me["display_name"], b["display_name"], b.get("invite_link"))
                is_sub = True
            else:
                msg, keyboard = viewer_not_sub(me["display_name"], b["display_name"], b.get("free_invite_link"))
                is_sub = False

            # ───────────────────────────────
            # Guardar en group_members
            # ───────────────────────────────
            try:
                await self.db.exec("""
                    INSERT INTO group_members (
                        group_id, telegram_id, username, twitch_login,
                        broadcaster_id, streamer_slug, joined_at, is_subscribed
                    ) VALUES (%s,%s,%s,%s,%s,%s,NOW(),%s)
                    ON CONFLICT (group_id, telegram_id)
                    DO UPDATE SET
                        username=EXCLUDED.username,
                        twitch_login=EXCLUDED.twitch_login,
                        is_subscribed=EXCLUDED.is_subscribed,
                        joined_at=NOW();
                """,
                    b["group_id"], row["telegram_id"], me["login"], me["id"],
                    b["broadcaster_id"], b["slug"], is_sub
                )
                logging.info(f"💾 Guardado en group_members: @{me['login']} ({row['telegram_id']}) - sub={is_sub}")
            except Exception as e:
                logging.warning(f"⚠️ No se pudo guardar en group_members: {e}")

            # Enviar mensaje final
            await self.send_tg(row["telegram_id"], msg, html=True, reply_markup=keyboard)

            return web.Response(
                text=f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                     "<h3>✅ Verificación completa. Puedes volver a Telegram.</h3>",
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_user:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)
