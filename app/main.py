import asyncio, os
from .bot import TwitchSubBot
from .db import Database

async def main():
    db = Database(os.getenv("DATABASE_URL"))
    bot = TwitchSubBot(db)
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
