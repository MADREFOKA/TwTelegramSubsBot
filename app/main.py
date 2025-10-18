# app/main.py
import asyncio
from app.db import Database
from app.bot import TwitchSubBot

async def main():
    db = Database()
    bot = TwitchSubBot(db)
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
