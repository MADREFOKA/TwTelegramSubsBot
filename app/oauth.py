# app/oauth.py
import time
import logging
import requests
from aiohttp import web

from app.utils.messages import setup_success, viewer_is_owner, viewer_is_sub, viewer_not_sub

class OAuthHandlers:
    def __init__(self, config, db, send_tg, refresh_twitch_token, days_until, cipher):
        """
        config: objeto con client_id, client_secret, base_url, bot_username, TW_API, TW_TOKEN
        db: instancia Database
        send_tg: fn async(chat_id, text, html=True/False)
        refresh_twitch_token: fn async(broadcaster_row)->access_token
        days_until: fn(str)->(str,int)
        cipher: TokenCipher (encrypt/decrypt) - se usará en fase 2
        """
        self.cfg = config
        self.db = db
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token
        self.days_until = days_until
        self.cipher = cipher

    async def twitch_callback_setup(self, request: web.Request):
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta code/state.", status=400)

            row = await self.db.one("SELECT * FROM oauth_states WHERE state=%s AND purpose='broadcaster_setup'", state)
            if not row:
                return web.Response(text="❌ Estado inválido.", status=400)

            # Intercambia code por token
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

            headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.cfg.client_id}
            me = requests.get(f"{self.cfg.TW_API}/users", headers=headers, timeout=15).json()["data"][0]
            slug = me["login"].lower()

            # (fase 2: cifrar aquí con cipher.encrypt)
            enc_access = access_token
            enc_refresh = refresh_token

            await self.db.exec("""
                INSERT INTO broadcasters(broadcaster_id, owner_telegram_id, access_token, refresh_token,
                                         token_obtained_at, token_expires_in, slug)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (broadcaster_id)
                DO UPDATE SET access_token=EXCLUDED.access_token,
                              refresh_token=EXCLUDED.refresh_token,
                              token_obtained_at=EXCLUDED.token_obtained_at,
                              token_expires_in=EXCLUDED.token_expires_in,
                              slug=EXCLUDED.slug
            """, me["id"], row["telegram_id"], enc_access, enc_refresh, int(time.time()), expires_in, slug)

            msg = setup_success(me["display_name"], self.cfg.bot_username, slug)
            await self.send_tg(row["telegram_id"], msg, html=True)

            return web.Response(
                text=f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                     "<h3>✅ Vinculación completada. Puedes volver a Telegram.</h3>",
                content_type="text/html"
            )
        except Exception as e:
            logging.exception("Error en twitch_callback_setup:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)

    async def twitch_callback_user(self, request: web.Request):
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta parámetros.", status=400)

            row = await self.db.one("SELECT * FROM oauth_states WHERE state=%s AND purpose='user_link'", state)
            if not row:
                return web.Response(text="❌ Estado inválido.", status=400)

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
            headers_user = {"Authorization": f"Bearer {access_token}", "Client-Id": self.cfg.client_id}
            me = requests.get(f"{self.cfg.TW_API}/users", headers=headers_user, timeout=15).json()["data"][0]

            await self.db.exec("""
                INSERT INTO telegram_users(telegram_id, username, linked_twitch_id)
                VALUES (%s,%s,%s)
                ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username, linked_twitch_id=EXCLUDED.linked_twitch_id
            """, row["telegram_id"], me["login"], me["id"])

            b = await self.db.one("SELECT * FROM broadcasters WHERE broadcaster_id=%s", row["broadcaster_id"])
            if not b:
                return web.Response(text="Streamer no encontrado.", status=400)

            # fase 2: descifrar si corresponde
            b_access = await self.refresh_twitch_token(b)
            headers_b = {"Authorization": f"Bearer {b_access}", "Client-Id": self.cfg.client_id}

            r = requests.get(f"{self.cfg.TW_API}/subscriptions/user",
                             headers=headers_b,
                             params={"broadcaster_id": b["broadcaster_id"], "user_id": me["id"]},
                             timeout=15)
            data_sub = r.json().get("data", [])

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
