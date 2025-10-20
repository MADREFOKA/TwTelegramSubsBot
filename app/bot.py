import asyncio
import logging
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from app.commands import Commands
from app.oauth import OAuthHandler
from app.auditor import Auditor
from app.dashboard import Dashboard
from app.security import TokenCipher  # ✅ esta es la versión correcta

class TwitchSubBot:
    def __init__(self, db):
        self.db = db
        self.cfg = db.cfg
        self.cipher = TokenCipher()  # ✅ en lugar de encrypt_token
        self.oauth = OAuthHandler(self.db, self.cfg, self.cipher)
        self.commands = Commands(self.db, self.cfg, self.cipher)
        self.auditor = Auditor(self.db)
        self.dashboard = Dashboard(self.db)

    async def start(self):
        app = ApplicationBuilder().token(self.cfg.token).build()

        app.add_handler(CommandHandler("setup", self.commands.cmd_setup))
        app.add_handler(CommandHandler("start", self.commands.cmd_start))
        app.add_handler(CommandHandler("setgroup", self.commands.cmd_setgroup))
        app.add_handler(CommandHandler("setfreegroup", self.commands.cmd_setfreegroup))
        app.add_handler(CommandHandler("regendashlink", self.commands.cmd_regendashlink))

        # Auditoría semanal
        self.auditor.schedule_weekly_audit(app.job_queue)

        web_app = web.Application()
        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/twitch/setup/callback", self.oauth.twitch_callback_setup)
        web_app.router.add_get("/twitch/callback", self.oauth.twitch_callback_user)
        web_app.router.add_get("/dashboard/{slug}", self.dashboard.page)
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo correctamente."))

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.cfg.port)
        await site.start()

        await app.initialize()
        await app.start()
        logging.info("💜 Bot y servidor web iniciados correctamente.")
        await asyncio.Event().wait()

    async def webhook_handler(self, request):
        data = await request.json()
        update = Update.de_json(data, self.commands.cfg.bot)
        await self.commands.cfg.bot.process_update(update)
        return web.Response(text="OK")
