import os, time, logging, asyncio, secrets, string, requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from aiohttp import web
from functools import wraps
from .db import Database

# -------------------------------
# LOGGING
# -------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def catch_exceptions(func):
    """Evita que un error en un comando apague el bot"""
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await func(self, update, context)
        except Exception as e:
            logging.error(f"❌ Error en {func.__name__}: {e}", exc_info=True)
            try:
                await update.message.reply_text("⚠️ Error interno. Intenta más tarde.")
            except:
                pass
    return wrapper


class TwitchSubBot:
    def __init__(self, db: Database):
        self.db = db
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.client_id = os.getenv("OAUTH_CLIENT_ID")
        self.client_secret = os.getenv("OAUTH_CLIENT_SECRET")
        self.base_url = os.getenv("BASE_URL")
        self.bot_username = os.getenv("BOT_USERNAME") or "TwSubCheck_bot"
        self.TW_API = "https://api.twitch.tv/helix"
        self.TW_AUTH = "https://id.twitch.tv/oauth2/authorize"
        self.TW_TOKEN = "https://id.twitch.tv/oauth2/token"
        self.SCOPE_SETUP = ["channel:read:subscriptions"]
        self.SCOPE_USER = ["user:read:email"]

        logging.info("⚙️ TwitchSubBot inicializado correctamente")

    # -------------------------------
    # UTILIDADES
    # -------------------------------
    def gen_state(self, n=32):
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    async def send_tg(self, chat_id, text, markdown=False):
        try:
            payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            if markdown:
                payload["parse_mode"] = "Markdown"
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logging.warning(f"Error enviando mensaje Telegram: {e}")

    # -------------------------------
    # COMANDOS BASE
    # -------------------------------
    @catch_exceptions
    async def cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await update.message.reply_text(f"✅ Hola, {user.first_name}! El bot está operativo.")
        logging.info(f"Test ejecutado por @{user.username}")

    @catch_exceptions
    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        start = time.time()
        msg = await update.message.reply_text("⏳ Pinging...")
        latency = (time.time() - start) * 1000
        await msg.edit_text(f"🏓 Pong! ({latency:.0f} ms)")
        logging.info(f"Ping ejecutado: {latency:.0f}ms")

    # -------------------------------
    # WEBHOOK HANDLER
    # -------------------------------
    async def webhook_handler(self, request: web.Request):
        """Procesa las actualizaciones entrantes del webhook de Telegram"""
        try:
            data = await request.json()
            logging.info(f"📩 Update recibido: {data}")

            if not hasattr(self, "app"):
                logging.warning("⚠️ self.app aún no existe.")
                return web.Response(text="App not initialized", status=503)

            bot_instance = getattr(self.app, "bot", None)
            if bot_instance is None:
                logging.warning("⚠️ self.app.bot aún no inicializado.")
                return web.Response(text="Bot not ready", status=503)

            update = Update.de_json(data, bot_instance)
            await self.app.process_update(update)
            logging.info("✅ Update procesado correctamente.")
            return web.Response(text="OK", status=200)

        except Exception as e:
            logging.error(f"❌ Error procesando webhook: {e}", exc_info=True)
            return web.Response(text=f"Error: {e}", status=500)

    # -------------------------------
    # ARRANQUE DE BOT Y SERVIDOR WEB
    # -------------------------------
    async def start(self):
        logging.info("🚀 Iniciando TwitchSubBot...")

        await self.db.init()

        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("test", self.cmd_test))
        app.add_handler(CommandHandler("ping", self.cmd_ping))
        self.app = app

        webhook_url = f"{self.base_url}/webhook"
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.token}/setWebhook",
                params={"url": webhook_url},
                timeout=10
            ).json()
            logging.info(f"📡 Webhook configurado → {resp}")
        except Exception as e:
            logging.error(f"❌ Error configurando webhook: {e}")

        web_app = web.Application()
        web_app["bot"] = app
        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo correctamente."))

        async def keepalive():
            while True:
                try:
                    requests.get(f"{self.base_url}/", timeout=5)
                except Exception:
                    pass
                await asyncio.sleep(25)

        asyncio.create_task(keepalive())

                runner = web.AppRunner(web_app)
        await runner.setup()

        import socket
        # Libera el puerto 8080 si otro proceso lo está usando
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("0.0.0.0", int(os.getenv("PORT", "8080"))))
            s.close()
        except OSError:
            logging.warning("⚠️ Puerto 8080 ocupado, esperando 3s...")
            await asyncio.sleep(3)

        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()

        await app.initialize()
        await app.start()

        logging.info("✅ Servidor web del bot iniciado y webhook activo.")
        await asyncio.Event().wait()
