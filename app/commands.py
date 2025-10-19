# app/commands.py
import time
import logging
import asyncio
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.utils.safe_exec import safe_execute


class Commands:
    def __init__(self, db, cfg, cipher):
        self.db = db
        self.cfg = cfg
        self.cipher = cipher

        # Constantes Twitch
        self.TW_API = cfg.TW_API
        self.TW_AUTH = cfg.TW_AUTH
        self.TW_TOKEN = cfg.TW_TOKEN
        self.SCOPE_SETUP = ["channel:read:subscriptions"]
        self.SCOPE_USER = ["user:read:email"]

    # ----------------------------------------------------------
    # 📤 UTILIDAD GENERAL — Enviar mensajes de Telegram con HTML
    # ----------------------------------------------------------
    async def send_tg(self, chat_id, text, html=False, buttons=None):
        """Envía mensajes a Telegram con HTML y botones opcionales."""
        from aiohttp import ClientSession
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = buttons.to_json()

        try:
            async with ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    # ----------------------------------------------------------
    # ⚙️ /SETUP — Inicia el vínculo del streamer
    # ----------------------------------------------------------
    @safe_execute()
    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /setup — inicia el vínculo del streamer con Twitch."""
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        user = update.effective_user
        state = self._gen_state()

        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at) VALUES(%s,%s,%s,%s)",
            state, user.id, "broadcaster_setup", int(time.time())
        )

        redirect_uri = f"{self.cfg.base_url}/twitch/setup/callback"
        scope = quote(" ".join(self.SCOPE_SETUP))
        url = (
            f"{self.TW_AUTH}?client_id={self.cfg.client_id}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code&scope={scope}&state={state}&force_verify=true"
        )

        keyboard = [[InlineKeyboardButton("💜 Vincular mi canal de Twitch", url=url)]]
        markup = InlineKeyboardMarkup(keyboard)

        msg = (
            "💜 <b>Configuración del streamer</b>\n\n"
            "Pulsa el botón inferior para vincular tu canal de Twitch con el bot.\n\n"
            "Una vez autorizado, podrás vincular tu grupo con <b>/setgroup</b> "
            "y los viewers podrán unirse automáticamente si son subs activos."
        )

        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode="HTML", reply_markup=markup)

    # ----------------------------------------------------------
    # 🎟️ /START — Para los viewers que se vinculan
    # ----------------------------------------------------------
    @safe_execute()
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start — para viewers que se vinculan."""
        if update.effective_chat.type != "private":
            return

        user = update.effective_user
        args = context.args if context.args else []
        if not args:
            return await update.message.reply_text("⚠️ Usa el enlace del streamer. Ejemplo: /start madrefoka")

        slug = args[0].lower()
        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b:
            return await update.message.reply_text("❌ Este streamer no está vinculado.")

        state = self._gen_state()
        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at,broadcaster_id) VALUES(%s,%s,%s,%s,%s)",
            state, user.id, "user_link", int(time.time()), b["broadcaster_id"]
        )

        redirect_uri = f"{self.cfg.base_url}/twitch/callback"
        scope = quote(" ".join(self.SCOPE_USER))
        url = (
            f"{self.TW_AUTH}?client_id={self.cfg.client_id}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code&scope={scope}&state={state}&force_verify=true"
        )

        keyboard = [[InlineKeyboardButton("💜 Vincular con Twitch", url=url)]]
        markup = InlineKeyboardMarkup(keyboard)

        msg = (
            f"🎟️ <b>Verificación de suscripción para {slug}</b>\n\n"
            "Pulsa el botón de abajo para vincular tu cuenta de Twitch "
            "y comprobar automáticamente si eres suscriptor activo.\n\n"
            "💡 Si lo eres, recibirás el enlace del grupo de subs al instante."
        )

        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode="HTML", reply_markup=markup)

    # ----------------------------------------------------------
    # 👑 /SETGROUP — Vincula el grupo premium del streamer
    # ----------------------------------------------------------
    @safe_execute()
    async def cmd_setgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Vincula grupo premium del streamer y envía el dashboard."""
        if update.effective_chat.type not in ("group", "supergroup"):
            return

        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec(
            "UPDATE broadcasters SET group_id=%s, invite_link=%s WHERE broadcaster_id=%s",
            update.effective_chat.id, link.invite_link, b["broadcaster_id"]
        )

        dashboard_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={b['dashboard_token']}"
        keyboard = [
            [InlineKeyboardButton("📊 Ver Dashboard", url=dashboard_link)],
            [InlineKeyboardButton("💜 Compartir enlace viewers", url=f"https://t.me/{self.cfg.bot_username}?start={b['slug']}")]
        ]
        markup = InlineKeyboardMarkup(keyboard)

        msg = (
            "✅ <b>Grupo premium vinculado correctamente</b>\n\n"
            "💜 Los subs podrán acceder automáticamente según su suscripción en Twitch.\n\n"
            "📊 Desde tu Dashboard puedes ver estadísticas, expulsiones y auditorías semanales."
        )

        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode="HTML", reply_markup=markup)

    # ----------------------------------------------------------
    # 🎁 /SETFREEGROUP — Grupo gratuito del streamer
    # ----------------------------------------------------------
    @safe_execute()
    async def cmd_setfreegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Vincula grupo gratuito del streamer."""
        if update.effective_chat.type not in ("group", "supergroup"):
            return

        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec(
            "UPDATE broadcasters SET free_group_id=%s, free_invite_link=%s WHERE broadcaster_id=%s",
            update.effective_chat.id, link.invite_link, b["broadcaster_id"]
        )

        await update.message.reply_text("✅ Grupo gratuito vinculado correctamente.")

    # ----------------------------------------------------------
    # 🛡️ /REGENDASHLINK — Regenerar el enlace del Dashboard
    # ----------------------------------------------------------
    @safe_execute()
    async def cmd_regendashlink(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Permite al streamer regenerar su enlace privado del dashboard."""
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        import secrets
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        new_token = secrets.token_urlsafe(32)
        await self.db.exec(
            "UPDATE broadcasters SET dashboard_token=%s WHERE broadcaster_id=%s",
            new_token, b["broadcaster_id"]
        )

        new_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={new_token}"
        keyboard = [[InlineKeyboardButton("📊 Abrir nuevo Dashboard", url=new_link)]]
        markup = InlineKeyboardMarkup(keyboard)

        msg = (
            "🔐 <b>Nuevo enlace del Dashboard generado</b>\n\n"
            "📊 Tu panel privado ha sido actualizado con un nuevo token seguro.\n\n"
            "🛡️ <i>El enlace anterior ha sido invalidado por seguridad.</i>"
        )

        await context.bot.send_message(chat_id=user.id, text=msg, parse_mode="HTML", reply_markup=markup)

    # ----------------------------------------------------------
    # 🧩 AUXILIAR — Generar state aleatorio para OAuth
    # ----------------------------------------------------------
    def _gen_state(self, n=32):
        import secrets, string
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))
