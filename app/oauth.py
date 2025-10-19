# app/oauth.py
import os
import logging
import aiohttp
from aiohttp import web
from app.security import TokenCipher

TW_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID") or os.getenv("OAUTH_CLIENT_ID")
TW_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET") or os.getenv("OAUTH_CLIENT_SECRET")
TW_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TW_API_URL = "https://api.twitch.tv/helix"

class OAuthHandler:
    def __init__(self, db):
        self.db = db
        self.cipher = TokenCipher()

    async def twitch_callback_setup(self, request):
        try:
            code = request.query.get("code")
            state = request.query.get("state")
            if not code or not state:
                return web.Response(status=400, text="Código o estado inválido.")

            state_row = await self.db.one("SELECT * FROM oauth_states WHERE state=%s", state)
            if not state_row:
                return web.Response(status=400, text="Estado no reconocido.")

            async with aiohttp.ClientSession() as session:
                payload = {
                    "client_id": TW_CLIENT_ID,
                    "client_secret": TW_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": f"{os.getenv('BASE_URL')}/twitch/setup/callback"
                }
                async with session.post(TW_TOKEN_URL, data=payload) as resp:
                    tokens = await resp.json()
                    access_token = tokens.get("access_token")
                    headers = {"Authorization": f"Bearer {access_token}", "Client-Id": TW_CLIENT_ID}
                    async with session.get(f"{TW_API_URL}/users", headers=headers) as r2:
                        data = await r2.json()
                        user = data["data"][0]

            slug = user["login"].lower()
            encrypted_token = self.cipher.encrypt(access_token)
            await self.db.exec(
                """INSERT INTO broadcasters (slug, owner_telegram_id, twitch_name, access_token)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (slug) DO UPDATE
                   SET access_token=excluded.access_token""",
                slug, state_row["telegram_id"], user["display_name"], encrypted_token
            )

            link = f"{os.getenv('BASE_URL')}/dashboard/{slug}?token={encrypted_token}"
            return web.Response(text=f"✅ Canal {slug} vinculado. Accede a tu dashboard: {link}")
        except Exception as e:
            logging.error(f"Error en twitch_callback_setup: {e}")
            return web.Response(status=500, text="Error interno en la vinculación.")
