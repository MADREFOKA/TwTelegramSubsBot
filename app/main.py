import os
import asyncio
from aiohttp import web
from .db import Database
from .bot import TwitchSubBot

async def main():
    db_url = os.getenv("DATABASE_URL")
    db = Database(db_url)
    bot = TwitchSubBot(db)

    async def ok(_):
        return web.Response(text="🟢 Bot operativo correctamente.")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_post("/webhook", bot.webhook_handler)
    app["bot_instance"] = bot  # por si el handler lo necesita

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    print("✅ Puerto 8080 abierto (Render detectará el servicio)")

    await bot.db.init()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
