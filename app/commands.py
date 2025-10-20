import time
import logging
import asyncio
import aiohttp
import secrets
import string
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from app.utils.safe_exec import safe_exec

class Commands:
    def __init__(self, db, cfg, cipher):
        self.db = db
        self.cfg = cfg
        self.cipher = cipher

        # Constantes Twitch
        self.TW_API = "https://api.twitch.tv/helix"
        self.TW_AUTH = "https://id.twitch.tv/oauth2/authorize"
        self.SCOPE_SETUP = ["channel:read:subscriptions"]
        self.SCOPE_USER = ["user:read:email"]

    # ---------------------------
    # Utilidad para enviar mensajes
    # ---------------------------
    async def send_tg(self, chat_id, text, buttons=None, html=False):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if html:
            payload["parse_mode"] = "HTML"
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}

        async with aiohttp.ClientSession() as session:
            await session.post(f"https://api.telegram.org/bot{self.cfg.token}/sendMessage", json=payload)

    def _gen_state(self, n=32):
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    # ---------------------------
    # /setup — streamer
    # ---------------------------
    @safe_exec()
    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        buttons = [[InlineKeyboardButton("💜 Vincular Canal de Twitch", url=url)]]
        await self.send_tg(
            user.id,
            "💜 <b>Vincula tu canal de Twitch para configurar tu bot</b>\n\n"
            "Tras autorizar, podrás conectar tus grupos premium y dashboard.",
            buttons=buttons, html=True
        )

    # ---------------------------
    # /start — viewer
    # ---------------------------
    @safe_exec()
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        buttons = [[InlineKeyboardButton("💜 Vincular mi Twitch", url=url)]]
        await self.send_tg(
            user.id,
            f"💜 <b>Conecta tu Twitch para verificar tu suscripción a {slug}</b>",
            buttons=buttons, html=True
        )

    # ---------------------------
    # /setgroup — grupo premium
    # ---------------------------
    @safe_exec()
    async def cmd_setgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        new_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={b['dashboard_token']}"
        buttons = [[InlineKeyboardButton("📊 Abrir Dashboard", url=new_link)]]
        await self.send_tg(
            user.id,
            "✅ <b>Grupo premium vinculado correctamente.</b>\n\n"
            "A partir de ahora, solo los subs de tu canal tendrán acceso.",
            buttons=buttons, html=True
        )

    # ---------------------------
    # /setfreegroup — grupo gratuito
    # ---------------------------
    @safe_exec()
    async def cmd_setfreegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # ---------------------------
    # /regendashlink — regenerar enlace privado
    # ---------------------------
    @safe_exec()
    async def cmd_regendashlink(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        buttons = [[InlineKeyboardButton("📊 Abrir Dashboard", url=new_link)]]
        await self.send_tg(
            user.id,
            "✅ <b>Enlace del dashboard actualizado</b>\n\n"
            "🛡️ El enlace anterior ha sido invalidado por seguridad.",
            buttons=buttons, html=True
        )
