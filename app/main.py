# app/main.py
import asyncio
import logging
from app.db import Database
from app.bot import TwitchSubBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

async def main():
    db = Database()
    bot = TwitchSubBot(db)
    try:
        await bot.start()
    except Exception as e:
        logging.error(f"❌ Error al iniciar el bot: {e}")

if __name__ == "__main__":
    asyncio.run(main())
