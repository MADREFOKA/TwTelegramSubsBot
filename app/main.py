# app/main.py
import asyncio
import logging
import os
from app.db import Database
from app.bot import TwitchSubBot
from app.security import TokenCipher
from app.utils.send_tg import send_tg
from app.utils.refresh_twitch import refresh_twitch_token
from app.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

async def main():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("❌ Falta la variable DATABASE_URL en el entorno.")

    cfg = Config()
    db = Database(dsn)
    await db.init()
    cipher = TokenCipher()

    bot = TwitchSubBot(
        db=db,
        cfg=cfg,
        cipher=cipher,
        refresh_twitch_token=refresh_twitch_token,
        send_tg=send_tg,
    )

    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
