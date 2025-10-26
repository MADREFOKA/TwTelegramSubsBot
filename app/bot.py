import os
import time
import logging
import asyncio
import requests
import aiohttp
import aiohttp_jinja2
import jinja2
from datetime import datetime, timezone
from aiohttp import web
from telegram.ext import ApplicationBuilder, CommandHandler, ChatMemberHandler, CallbackQueryHandler
from telegram import Update
from app.db import Database
from app.security import TokenCipher
from app.commands import Commands
from app.oauth import OAuthHandlers
from app.auditor import Auditor
from app.dashboard import Dashboard


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
        self.app = None

        # Componentes
        self.cmds = Commands(self.db, self.cfg, self.cipher)
        self.oauth = OAuthHandlers(
            self.cfg, self.db, self.send_tg, self.refresh_twitch_token, self.days_until, self.cipher
        )
        self.auditor = Auditor(self.db, self.cfg, self.send_tg, self.refresh_twitch_token)
        self.dashboard = Dashboard(self.db)

    # app/bot.py (fragmento dentro de la clase TwitchSubBot)
    async def send_tg(self, chat_id, text, md=False, html=False, disable_preview=False, reply_markup=None):
        """Envia mensajes a Telegram de forma segura."""
        import aiohttp
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
            "disable_notification": False,
        }
    
        if md:
            payload["parse_mode"] = "Markdown"
        elif html:
            payload["parse_mode"] = "HTML"
    
        if reply_markup:
            payload["reply_markup"] = reply_markup.to_dict()
    
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    async def refresh_twitch_token(self, broadcaster_row: dict) -> str:
        """Renueva tokens de Twitch si han caducado."""
        access_token = self.cipher.decrypt(broadcaster_row["access_token"])
        refresh_token = self.cipher.decrypt(broadcaster_row.get("refresh_token"))
        obtained = broadcaster_row["token_obtained_at"]
        expires = broadcaster_row["token_expires_in"]

        if not refresh_token:
            return access_token
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
                enc_access = self.cipher.encrypt(access_token)
                enc_refresh = self.cipher.encrypt(refresh_token)
                await self.db.exec("""
                    UPDATE broadcasters
                    SET access_token=%s, refresh_token=%s, token_obtained_at=%s, token_expires_in=%s
                    WHERE broadcaster_id=%s
                """, enc_access, enc_refresh, int(time.time()), data.get("expires_in", expires),
                   broadcaster_row["broadcaster_id"])
        except Exception as e:
            logging.warning(f"Error al refrescar token: {e}")
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

    async def webhook_handler(self, request: web.Request):
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if token and token != self.cfg.secret_token:
            return web.Response(text="Unauthorized", status=403)
        try:
            data = await request.json()
            update = Update.de_json(data, self.app.bot)
            await self.app.process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            logging.exception("Error procesando webhook:")
            return web.Response(text=f"Error interno: {e}", status=500)

    async def start(self):
        await self.db.init()
        app = ApplicationBuilder().token(self.cfg.token).build()
        self.app = app

        # Comandos
        app.add_handler(CommandHandler("setup", self.cmds.cmd_setup))
        app.add_handler(CommandHandler("start", self.cmds.cmd_start))
        app.add_handler(CommandHandler("setgroup", self.cmds.cmd_setgroup))
        app.add_handler(CommandHandler("setfreegroup", self.cmds.cmd_setfreegroup))
        app.add_handler(CommandHandler("regendashlink", self.cmds.cmd_regendashlink))
        app.add_handler(CommandHandler("auditnow", self.auditor.cmd_auditnow))
        app.add_handler(CommandHandler("priv_add", self.cmds.cmd_priv_add))
        app.add_handler(CommandHandler("priv_delete", self.cmds.cmd_priv_delete))
        app.add_handler(CommandHandler("priv_list", self.cmds.cmd_priv_list))
        app.add_handler(CallbackQueryHandler(self.cmds.on_callback, pattern="^guide_usage$"))

        try:
            self.auditor.schedule_weekly_audit(app.job_queue)
        except Exception as e:
            logging.warning(f"No se pudo programar la auditoría semanal: {e}")

        # Webhook
        webhook_url = f"{self.cfg.base_url}/webhook"
        requests.get(
            f"https://api.telegram.org/bot{self.cfg.token}/setWebhook",
            params={
                "url": webhook_url,
                "secret_token": self.cfg.secret_token,
                "allowed_updates": ["message", "chat_member"],
                "drop_pending_updates": True,
            },
            timeout=10,
        )

        # Servidor web
        web_app = web.Application()
        web_app["bot"] = app
        template_path = os.path.join(os.path.dirname(__file__), "templates")
        aiohttp_jinja2.setup(web_app, loader=jinja2.FileSystemLoader(template_path))

        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/twitch/setup/callback", self.oauth.twitch_callback_setup)
        web_app.router.add_get("/twitch/callback", self.oauth.twitch_callback_user)
        web_app.router.add_get("/dashboard/{slug}", self.dashboard.page, name="dashboard_page")
        web_app.router.add_get("/dashboard/auditnow/{slug}", self.dashboard.trigger_audit)
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo correctamente."))

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()

        await app.initialize()
        await app.start()
        logging.info("💜 Bot y servidor web iniciados correctamente.")
        await asyncio.Event().wait()
