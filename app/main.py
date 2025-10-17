import os
import asyncio
from aiohttp import web
from .db import Database
from .bot import TwitchSubBot

async def warmup_server():
    """Servidor mínimo para que Render detecte el puerto"""
    async def ok(_):
        return web.Response(text="🟢 Bot en ejecución (Warmup OK)")

    app = web.Application()
    app.router.add_get("/", ok)
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Puerto {port} abierto (warmup Render)")
    return runner

async def launch_bot():
    """Inicia el bot completo en paralelo"""
    db_url = os.getenv("DATABASE_URL")
    db = Database(db_url)
    bot = TwitchSubBot(db)
    await bot.start()

async def main():
    await warmup_server()
    await asyncio.sleep(5)
    asyncio.create_task(launch_bot())
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
