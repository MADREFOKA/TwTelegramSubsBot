# app/oauth.py
import time
import logging
import requests
import secrets
from aiohttp import web

from app.utils.messages import setup_success, viewer_is_owner, viewer_is_sub, viewer_not_sub


class OAuthHandlers:
    def __init__(self, config, db, send_tg, refresh_twitch_token, days_until, cipher):
        """
        Manejador de callbacks OAuth de Twitch.
        - config: datos de entorno (client_id, client_secret, base_url, etc.)
        - db: instancia de Database
        - send_tg: función async(chat_id, texto, html/md)
        - refresh_twitch_token: refresca token de broadcaster
        - days_until: calcula días hasta renovación
        - cipher: TokenCipher (Fernet) para cifrado/descifrado
        """
        self.cfg = config
        self.db = db
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token
        self.days_until = days_until
        self.cipher = cipher

    # ----------------------------------------------------------------------
    # CALLBACK STREAMER — /setup
    # ----------------------------------------------------------------------
    async def twitch_callback_setup(self, request: web.Request):
        """Guarda tokens del streamer tras autorizar la app (fase setup)."""
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta code/state.", status=400)

            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='broadcaster_setup'", state
            )
            if not row:
                return web.Response(text="❌ Estado inválido o expirado.", status=400)

            # Intercambio del código por tokens
            data = requests.post(self.cfg.TW_TOKEN, data={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{self.cfg.base_url}/twitch/setup/callback"
            }, timeout=15).json()

            if "access_token" not in data:
                return web.Response(text=f"Error token: {data}", status=400)

            access_token = data["access_token"]
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 0)

            # Pedir datos del streamer
            headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.cfg.client_id}
            me = requests.get(f"{self.cfg.TW_API}/users", headers=headers, timeout=15).json()["data"][0]
            slug = me["login"].lower()

            # 🔒 Cifrar tokens antes de guardar
            enc_access = self.cipher.encrypt(access_token)
            enc_refresh = self.cipher.encrypt(refresh_token)

            # Crear token de dashboard privado
            dashboard_token = secrets.token_urlsafe(32)

            # Guardar en base de datos
            await self.db.exec("""
                INSERT INTO broadcasters(broadcaster_id, owner_telegram_id, access_token, refresh_token,
                                         token_obtained_at, token_expires_in, slug, dashboard_token)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (broadcaster_id)
                DO UPDATE SET access_token=EXCLUDED.access_token,
                              refresh_token=EXCLUDED.refresh_token,
                              token_obtained_at=EXCLUDED.token_obtained_at,
                              token_expires_in=EXCLUDED.token_expires_in,
                              slug=EXCLUDED.slug,
                              dashboard_token=EXCLUDED.dashboard_token
            """, me["id"], row["telegram_id"], enc_access, enc_refresh,
                 int(time.time()), expires_in, slug, dashboard_token)

            # Enviar mensaje de éxito con su dashboard privado
            msg = setup_success(
                me["display_name"], self.cfg.bot_username, slug, self.cfg.base_url, dashboard_token
            )
            await self.send_tg(row["telegram_id"], msg, html=True)

            return web.Response(
                text=f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                     "<h3>✅ Vinculación completada. Puedes volver a Telegram.</h3>",
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_setup:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)

    # ----------------------------------------------------------------------
    # CALLBACK VIEWER — /start
    # ----------------------------------------------------------------------
    async def twitch_callback_user(self, request: web.Request):
        """Vincula viewers y verifica suscripción."""
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta parámetros.", status=400)

            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='user_link'", state
            )
            if not row:
                return web.Response(text="❌ Estado inválido o expirado.", status=400)

            # Intercambio de código por token temporal de usuario
            data = requests.post(self.cfg.TW_TOKEN, data={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.cfg.base_url}/twitch/callback"
            }, timeout=15).json()

            if "access_token" not in data:
                return web.Response(text=f"Error al obtener token: {data}", status=400)

            # Obtener info del usuario de Twitch
            access_token = data["access_token"]
            headers_user = {"Authorization": f"Bearer {access_token}", "Client-Id": self.cfg.client_id}
            me = requests.get(f"{self.cfg.TW_API}/users", headers=headers_user, timeout=15).json()["data"][0]

            # Guardar o actualizar el vínculo Telegram ↔ Twitch
            await self.db.exec("""
                INSERT INTO telegram_users(telegram_id, username, linked_twitch_id)
                VALUES (%s,%s,%s)
                ON CONFLICT (telegram_id)
                DO UPDATE SET username=EXCLUDED.username,
                              linked_twitch_id=EXCLUDED.linked_twitch_id
            """, row["telegram_id"], me["login"], me["id"])

            # Buscar streamer correspondiente
            b = await self.db.one("SELECT * FROM broadcasters WHERE broadcaster_id=%s", row["broadcaster_id"])
            if not b:
                return web.Response(text="Streamer no encontrado.", status=400)

            # 🔓 Descifrar tokens de streamer antes de usarlos
            b_access = self.cipher.decrypt(b["access_token"])
            headers_b = {"Authorization": f"Bearer {b_access}", "Client-Id": self.cfg.client_id}

            # Verificar si el viewer es suscriptor
            r = requests.get(f"{self.cfg.TW_API}/subscriptions/user",
                             headers=headers_b,
                             params={"broadcaster_id": b["broadcaster_id"], "user_id": me["id"]},
                             timeout=15)
            data_sub = r.json().get("data", [])

            # Preparar respuesta según el tipo de usuario
            if me["id"] == b["broadcaster_id"]:
                msg = viewer_is_owner(me["display_name"], b["slug"], b.get("invite_link"))
            elif data_sub:
                sub = data_sub[0]
                renew, days = self.days_until(sub.get("ends_at", ""))
                msg = viewer_is_sub(me["display_name"], b["slug"], renew, days, b.get("invite_link"))
            else:
                msg = viewer_not_sub(me["display_name"], b["slug"], b.get("free_invite_link"))

            await self.send_tg(row["telegram_id"], msg, html=True)

            return web.Response(
                text=f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                     "<h3>✅ Verificación completada. Puedes volver a Telegram.</h3>",
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_user:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)
