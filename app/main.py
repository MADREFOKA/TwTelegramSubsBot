# app/main.py
import asyncio
import logging
import os
from app.db import Database
from app.bot import TwitchSubBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

async def main():
    # 🗄️ Obtiene el DSN de las variables de entorno
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("❌ Falta la variable DATABASE_URL en el entorno.")

    db = Database(dsn)
    await db.init()

    bot = TwitchSubBot(db)
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
