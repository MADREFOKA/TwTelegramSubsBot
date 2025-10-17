import os, asyncio
from .db import Database
from .bot import TwitchSubBot

async def main():
    db_url = os.getenv("DATABASE_URL")
    db = Database(db_url) 
    bot = TwitchSubBot(db)
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
