# app/bot.py
import os
import time
import logging
import asyncio
import requests
import aiohttp
from datetime import datetime, timezone
from aiohttp import web
from telegram.ext import ApplicationBuilder, CommandHandler

from app.db import Database
from app.security import TokenCipher
from app.commands import Commands
from app.oauth import OAuthHandlers

class Config:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.client_id = os.getenv("OAUTH_CLIENT_ID")
        self.client_secret = os.getenv("OAUTH_CLIENT_SECRET")
        self.base_url = os.getenv("BASE_URL")
        self.bot_username = os.getenv("BOT_USERNAME") or "TwSubCheck_bot"
        self.secret_token = os.getenv("FLASK_SECRET")
        self.TW_API = "https://api.twitch.tv/helix"
        self.TW_AUTH = "https://id.twitch.tv/oauth2/authorize"
        self.TW_TOKEN = "https://id.twitch.tv/oauth2/token"

class TwitchSubBot:
    def __init__(self, db: Database):
        self.db = db
        self.cfg = Config()
        self.cipher = TokenCipher()
        self.app = None  # telegram app

    # -------- utilidades compartidas
    async def send_tg(self, chat_id, text, md=False, html=False):
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if md:
            payload["parse_mode"] = "Markdown"
        elif html:
            payload["parse_mode"] = "HTML"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram  {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    async def refresh_twitch_token(self, broadcaster_row: dict) -> str:
        """Devuelve access_token válido (aquí sin cifrado; en la fase 2 lo añadimos)."""
        access_token = broadcaster_row["access_token"]
        refresh_token = broadcaster_row.get("refresh_token")
        obtained = broadcaster_row["token_obtained_at"]
        expires = broadcaster_row["token_expires_in"]

        if not refresh_token:
            return access_token
        # Si faltan más de 24h, seguimos usando actual
        if time.time() - obtained < max(0, expires - 86400):
            return access_token

        try:
            data = requests.post(self.cfg.TW_TOKEN, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret
            }, timeout=15).json()
            if "access_token" in data:
                access_token = data["access_token"]
                refresh_token = data.get("refresh_token", refresh_token)
                await self.db.exec("""
                    UPDATE broadcasters
                    SET access_token=%s, refresh_token=%s, token_obtained_at=%s, token_expires_in=%s
                    WHERE broadcaster_id=%s
                """, access_token, refresh_token, int(time.time()), data.get("expires_in", expires),
                   broadcaster_row["broadcaster_id"])
        except Exception as e:
            logging.warning(f"Error refresh token: {e}")
        return access_token

    def days_until(self, iso_ts: str):
        try:
            renew_time = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            diff = renew_time - now
            days = max(diff.days, 0)
            return renew_time.strftime("%d/%m/%Y %H:%M UTC"), days
        except Exception:
            return "desconocida", 0

    # -------- webhooks
    async def webhook_handler(self, request: web.Request):
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if token and token != self.cfg.secret_token:
            return web.Response(text="Unauthorized", status=403)
        data = await request.json()
        update = self.app.update_queue.bot._bot.update_de_json(data, self.app.bot)
        await self.app.process_update(update)
        return web.Response(text="OK")

    async def start(self):
        await self.db.init()

        # Instancias de servicios
        commands = Commands(self.cfg, self.db, self.send_tg, self.refresh_twitch_token, self.days_until)
        oauth = OAuthHandlers(self.cfg, self.db, self.send_tg, self.refresh_twitch_token, self.days_until, self.cipher)

        # Telegram
        tg_app = ApplicationBuilder().token(self.cfg.token).build()
        tg_app.add_handler(CommandHandler("setup", commands.setup))
        tg_app.add_handler(CommandHandler("start", commands.start))
        tg_app.add_handler(CommandHandler("checkme", commands.checkme))
        tg_app.add_handler(CommandHandler("setgroup", commands.setgroup))
        tg_app.add_handler(CommandHandler("setfreegroup", commands.setfreegroup))
        tg_app.add_handler(CommandHandler("auditnow", commands.auditnow))
        tg_app.add_handler(CommandHandler("priv_add", commands.priv_add))
        tg_app.add_handler(CommandHandler("priv_remove", commands.priv_remove))
        tg_app.add_handler(CommandHandler("priv_list", commands.priv_list))
        self.app = tg_app

        # Webhook
        webhook_url = f"{self.cfg.base_url}/webhook"
        requests.get(
            f"https://api.telegram.org/bot{self.cfg.token}/setWebhook",
            params={"url": webhook_url, "secret_token": self.cfg.secret_token, "allowed_updates": ["message"]},
            timeout=10
        )

        # Servidor web
        web_app = web.Application()
        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/twitch/setup/callback", oauth.twitch_callback_setup)
        web_app.router.add_get("/twitch/callback", oauth.twitch_callback_user)
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo correctamente."))

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()

        await tg_app.initialize()
        await tg_app.start()
        logging.info(f"✅ Webhook activo en: {webhook_url}")
        await asyncio.Event().wait()
