# app/main.py
import asyncio
import logging
import os
from aiohttp import web
from app.db import Database
from app.bot import TwitchSubBot
from app.dashboard import Dashboard
from app.oauth import OAuthHandlers
from app.auditor import Auditor

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

async def main():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("❌ Falta la variable DATABASE_URL en el entorno.")

    db = Database(dsn)
    await db.init()

    bot = TwitchSubBot(db)

    cfg = bot.cfg
    send_tg = bot.send_tg
    refresh_twitch_token = bot.refresh_twitch_token
    days_until = getattr(bot, "days_until", None)
    cipher = getattr(bot, "cipher", None)

    dashboard = Dashboard(db)
    oauth_handlers = OAuthHandlers(cfg, db, send_tg, refresh_twitch_token, days_until, cipher)
    auditor = Auditor(db, cfg, send_tg, refresh_twitch_token)

    if hasattr(bot, "web"):
        bot.web["bot"] = bot
        bot.web["refresh_twitch_token"] = refresh_twitch_token

        bot.web.add_routes([
            web.get("/dashboard/{slug}", dashboard.page),
            web.post("/dashboard/{slug}/audit", dashboard.trigger_audit),
            web.get("/twitch/setup/callback", oauth_handlers.twitch_callback_setup),
            web.get("/twitch/callback", oauth_handlers.twitch_callback_user),
        ])

    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
