# app/oauth.py
import time
import logging
import requests

from aiohttp import web

from app.auditor import fetch_all_subs
from app.utils.messages import (
    setup_success,
    viewer_is_owner,
    viewer_is_sub,
    viewer_not_sub,
)


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

            # Enviar mensaje sin previsualización del enlace
            await self.send_tg(
                row["telegram_id"],
                msg,
                html=True,
                disable_preview=True,
                reply_markup=keyboard
            )

            return web.Response(
                text=(
                    f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                    "<h3>✅ Vinculación completada. Puedes volver a Telegram.</h3>"
                ),
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_setup:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)

    # ───────────────────────────────────────────────
    # INVITACIÓN PREMIUM DE UN SOLO USO (member_limit=1)
    # ───────────────────────────────────────────────
    def _create_one_time_premium_invite(self, group_id: int, ttl_seconds: int = 600):
        if not group_id:
            return None
        try:
            url = f"https://api.telegram.org/bot{self.cfg.token}/createChatInviteLink"
            payload = {
                "chat_id": group_id,
                "member_limit": 1,  # ✅ 1 solo uso
                "expire_date": int(time.time()) + ttl_seconds,
                "name": f"premium-1use-{int(time.time())}",
            }
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                logging.warning(f"createChatInviteLink {r.status_code}: {r.text}")
                return None
            return r.json().get("result", {}).get("invite_link")
        except Exception as e:
            logging.warning(f"⚠️ Error creando link 1-uso: {e}")
            return None

    # ───────────────────────────────────────────────
    # CALLBACK USUARIO / VIEWER
    # ───────────────────────────────────────────────
    async def twitch_callback_user(self, request: web.Request):
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

            # Datos del usuario de Twitch
            me = requests.get(
                f"{self.cfg.TW_API}/users",
                headers=headers_user,
                timeout=15
            ).json()["data"][0]

            # Broadcaster asociado
            b = await self.db.one(
                "SELECT * FROM broadcasters WHERE broadcaster_id=%s",
                row["broadcaster_id"]
            )
            if not b:
                return web.Response(text="Streamer no encontrado.", status=400)

            # Token del broadcaster (para fallback/validaciones)
            b_access = await self.refresh_twitch_token(b)
            headers_b = {
                "Authorization": f"Bearer {b_access}",
                "Client-Id": self.cfg.client_id
            }

            # ───────────────────────────────────────
            # Verificar suscripción (robusto + fallback)
            # ───────────────────────────────────────
            data_sub = []
            try:
                r = requests.get(
                    f"{self.cfg.TW_API}/subscriptions/user",
                    headers=headers_user,
                    params={"broadcaster_id": b["broadcaster_id"], "user_id": me["id"]},
                    timeout=15
                )
                if r.status_code == 200:
                    res_json = r.json()
                    data_sub = res_json.get("data", [])
                    logging.info(f"Twitch subscriptions/user OK: {res_json}")
                else:
                    logging.warning(f"Twitch subscriptions/user {r.status_code}: {r.text}")
            except Exception as e:
                logging.warning(f"Twitch subscriptions/user exception: {e}")

            # Fallback: lista completa del broadcaster (evita falsos NO SUB)
            if (me["id"] != b["broadcaster_id"]) and not data_sub:
                subs_set, err = fetch_all_subs(b["broadcaster_id"], headers_b)

                # Si token broadcaster falló, reintenta tras refresh
                if err and err[0] == 401:
                    b_access = await self.refresh_twitch_token(b)
                    headers_b["Authorization"] = f"Bearer {b_access}"
                    subs_set, err = fetch_all_subs(b["broadcaster_id"], headers_b)

                if (not err) and subs_set and (str(me["id"]) in subs_set):
                    data_sub = [{"fallback": True}]

            # Link premium 1-uso (solo si hay group_id)
            premium_link = None
            if b.get("group_id"):
                premium_link = self._create_one_time_premium_invite(b["group_id"], ttl_seconds=600)
            
                # Fallback: si no se pudo crear 1-uso, usar el link persistente guardado
                if not premium_link:
                    premium_link = b.get("invite_link")
                    
            # -------- Mensajes para el usuario --------
            if me["id"] == b["broadcaster_id"]:
                is_sub = True
                msg, keyboard = viewer_is_owner(me["display_name"], b["display_name"], premium_link)
            elif data_sub:
                is_sub = True
                msg, keyboard = viewer_is_sub(me["display_name"], b["display_name"], premium_link)
            else:
                is_sub = False
                msg, keyboard = viewer_not_sub(me["display_name"], b["display_name"], b.get("free_invite_link"))

            # ───────────────────────────────────────
            # Guardar / actualizar en telegram_users
            # ───────────────────────────────────────
            try:
                tu = await self.db.one(
                    "SELECT * FROM telegram_users WHERE telegram_id=%s",
                    row["telegram_id"]
                )

                if tu:
                    # Ya hay algún registro para este telegram_id
                    if tu.get("streamer_id") == b["broadcaster_id"]:
                        # Mismo streamer → actualizar
                        await self.db.exec("""
                            UPDATE telegram_users
                            SET linked_twitch_id=%s,
                                twitch_login=%s,
                                is_subscribed=%s
                            WHERE telegram_id=%s AND streamer_id=%s
                        """, me["id"], me["login"], is_sub,
                             row["telegram_id"], b["broadcaster_id"])
                    else:
                        # Distinto streamer → nueva fila para este streamer
                        await self.db.exec("""
                            INSERT INTO telegram_users (
                                telegram_id, username, first_name, last_name,
                                linked_twitch_id, twitch_login, is_subscribed, streamer_id
                            )
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """, row["telegram_id"], tu.get("username"), tu.get("first_name"),
                             tu.get("last_name"), me["id"], me["login"], is_sub, b["broadcaster_id"])
                else:
                    # No existía → crear primera fila para este user/streamer
                    await self.db.exec("""
                        INSERT INTO telegram_users (
                            telegram_id, username, first_name, last_name,
                            linked_twitch_id, twitch_login, is_subscribed, streamer_id
                        )
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, row["telegram_id"], None, None, None,
                         me["id"], me["login"], is_sub, b["broadcaster_id"])

                logging.info(
                    f"💾 telegram_users actualizado → TG:{row['telegram_id']} TW:{me['login']} Streamer:{b['broadcaster_id']}"
                )
            except Exception as e:
                logging.warning(f"⚠️ No se pudo actualizar telegram_users: {e}")

            # ──────────────────────────────────────────────────────
            # Guardar / actualizar en telegram_users (por streamer)
            # ──────────────────────────────────────────────────────
            try:
                tu = await self.db.one(
                    "SELECT * FROM telegram_users WHERE telegram_id=%s AND streamer_id=%s",
                    row["telegram_id"], b["broadcaster_id"]
                )
            
                if tu:
                    # Mismo streamer → actualizar
                    await self.db.exec("""
                        UPDATE telegram_users
                        SET linked_twitch_id=%s,
                            twitch_login=%s,
                            is_subscribed=%s
                        WHERE telegram_id=%s AND streamer_id=%s
                    """, me["id"], me["login"], is_sub,
                         row["telegram_id"], b["broadcaster_id"])
                else:
                    # No existía fila para este streamer → crear
                    await self.db.exec("""
                        INSERT INTO telegram_users (
                            telegram_id, username, first_name, last_name,
                            linked_twitch_id, twitch_login, is_subscribed, streamer_id
                        )
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, row["telegram_id"], None, None, None,
                         me["id"], me["login"], is_sub, b["broadcaster_id"])
            
                logging.info(
                    f"💾 telegram_users actualizado → TG:{row['telegram_id']} TW:{me['login']} Streamer:{b['broadcaster_id']}"
                )
            except Exception as e:
                logging.warning(f"⚠️ No se pudo actualizar telegram_users: {e}")

            # ───────────────
            # LÓGICA FINAL
            # ───────────────
            if not is_sub:
                if b.get("free_invite_link"):
                    await self.send_tg(
                        row["telegram_id"],
                        msg,
                        html=True,
                        reply_markup=keyboard
                    )
                else:
                    await self.send_tg(
                        row["telegram_id"],
                        (
                            f"❌ {me['display_name']}, no tienes una suscripción activa a "
                            f"{b['display_name']}.\n\n"
                            "Cuando te suscribas, vuelve a usar /start con este bot para acceder al grupo premium."
                        ),
                        html=False
                    )
            else:
                await self.send_tg(
                    row["telegram_id"],
                    msg,
                    html=True,
                    reply_markup=keyboard
                )

            # Página final
            return web.Response(
                text=(
                    f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                    "<h3>✅ Verificación completa. Puedes volver a Telegram.</h3>"
                ),
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_user:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)
