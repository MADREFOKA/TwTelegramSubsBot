import os, asyncio, threading
from aiohttp import web
from .db import Database
from .bot import TwitchSubBot

# --- Servidor mínimo para que Render detecte el puerto ---
async def warmup_server():
    async def ok(_):
        return web.Response(text="🟢 Warmup OK")
    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    print("✅ Puerto 8080 abierto (warmup Render)")

# --- Lanza el bot en segundo plano ---
async def run_bot():
    db_url = os.getenv("DATABASE_URL")
    db = Database(db_url)
    bot = TwitchSubBot(db)
    await bot.start()

def start_bot_in_thread():
    asyncio.run(run_bot())

async def main():
    # Inicia servidor de detección para Render inmediatamente
    await warmup_server()

    # Arranca el bot en segundo plano (no bloquea)
    threading.Thread(target=start_bot_in_thread, daemon=True).start()

    # Mantén vivo el proceso principal
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
