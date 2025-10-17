import asyncio
from .db import Database
from .bot import TwitchSubBot

db = Database()
bot = TwitchSubBot(db)

async def main():
    # Lanza primero solo el servidor aiohttp para que Render detecte el puerto
    server_task = asyncio.create_task(bot.start())
    # Espera unos segundos y confirma que Render ya detectó el puerto
    await asyncio.sleep(5)
    # Mantén la app viva
    await server_task
if __name__ == "__main__":
    asyncio.run(main())
