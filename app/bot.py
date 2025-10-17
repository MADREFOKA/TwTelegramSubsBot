import os, time, logging, asyncio, secrets, string, requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from aiohttp import web
from functools import wraps
from .db import Database

# -------------------------------
# CONFIGURACIÓN DE LOGGING
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
        """Genera un token aleatorio para estados OAuth"""
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    async def send_tg(self, chat_id, text, markdown=False):
        """Envía mensajes por la API de Telegram"""
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
        """Comando de prueba: verifica conexión y BD"""
        user = update.effective_user
        await update.message.reply_text(f"✅ Hola, {user.first_name}! El bot está operativo.")
        logging.info(f"Test ejecutado por @{user.username}")

    @catch_exceptions
    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando simple para medir latencia"""
        start = time.time()
        msg = await update.message.reply_text("⏳ Pinging...")
        end = time.time()
        latency = (end - start) * 1000
        await msg.edit_text(f"🏓 Pong! ({latency:.0f} ms)")
        logging.info(f"Ping ejecutado: {latency:.0f}ms")

    # -------------------------------
    # ARRANQUE DE WEBHOOK Y BOT
    # -------------------------------
    async def webhook_handler(self, request: web.Request):
        """Webhook de Telegram"""
        try:
            data = await request.json()
            logging.info(f"📩 Update recibido: {data}")
            update = Update.de_json(data, request.app["bot"].bot)
            await request.app["bot"].process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            logging.error(f"❌ Error procesando webhook: {e}")
            return web.Response(text=f"Error: {e}", status=500)

        async def start(self):
        """Inicia el bot y el servidor web aiohttp"""
        logging.info("🚀 Iniciando TwitchSubBot...")

        # Inicializa BD
        await self.db.init()

        # Crea app de Telegram
        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("test", self.cmd_test))
        app.add_handler(CommandHandler("ping", self.cmd_ping))

        # ⚙️ Inicializamos primero el bot
        await app.initialize()
        await app.start()

        # Configura webhook en Telegram
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

        # Crea servidor aiohttp
        web_app = web.Application()
        web_app["bot"] = app

        @web.middleware
        async def allow_cors(request, handler):
            response = await handler(request)
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response

        web_app.middlewares.append(allow_cors)
        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo"))

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()

        logging.info("✅ Servidor iniciado correctamente en Render")

        # Mantiene el servicio vivo
        await asyncio.Event().wait()
