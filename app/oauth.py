import os
import time
import json
import logging
import requests
from aiohttp import web
from app.utils.safe_exec import safe_exec


class OAuthHandlers:
    def __init__(self, cfg, db, send_tg, refresh_token_fn, days_until_fn, cipher):
        self.cfg = cfg
        self.db = db
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_token_fn
        self.days_until = days_until_fn
        self.cipher = cipher

    # ---------------------------
    # Callback del streamer (/twitch/setup/callback)
    # ---------------------------
    @safe_exec()
    async def twitch_callback_setup(self, request: web.Request):
        params = request.rel_url.query
        code = params.get("code")
        state = params.get("state")
        if not code or not state:
            return web.Response(text="❌ Parámetros inválidos.", status=400)

        token_data = await self._exchange_code(code)
        if not token_data:
            return web.Response(text="❌ Error autenticando con Twitch.", status=400)

        headers = {"Authorization": f"Bearer {token_data['access_token']}", "Client-Id": self.cfg.client_id}
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.twitch.tv/helix/users", headers=headers) as resp:
                user = (await resp.json())["data"][0]

        slug = user["login"].lower()
        encrypted = self.cipher.encrypt(token_data["access_token"])
        new_token = self.cipher.encrypt(token_data["refresh_token"])

        await self.db.exec("""
            INSERT INTO broadcasters (slug, twitch_id, twitch_name, access_token, refresh_token, owner_telegram_id, dashboard_token)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (slug) DO UPDATE SET
                access_token=EXCLUDED.access_token,
                refresh_token=EXCLUDED.refresh_token
        """, slug, user["id"], user["display_name"], encrypted, new_token, None, "")

        return web.Response(text="✅ Canal vinculado correctamente. Ya puedes usar /setgroup en tu grupo de Telegram.")

    # ---------------------------
    # Callback del usuario (/twitch/callback)
    # ---------------------------
    @safe_exec()
    async def twitch_callback_user(self, request: web.Request):
        params = request.rel_url.query
        code = params.get("code")
        state = params.get("state")
        if not code or not state:
            return web.Response(text="❌ Parámetros inválidos.", status=400)

        token_data = await self._exchange_code(code)
        if not token_data:
            return web.Response(text="❌ Error autenticando con Twitch.", status=400)

        return web.Response(text="✅ Vinculación completada correctamente. Ya puedes volver a Telegram.")

    async def _exchange_code(self, code):
        data = {
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": f"{self.cfg.base_url}/twitch/setup/callback",
            "code": code,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://id.twitch.tv/oauth2/token", data=data) as resp:
                if resp.status != 200:
                    logging.error(f"Twitch token exchange failed: {await resp.text()}")
                    return None
                return await resp.json()
