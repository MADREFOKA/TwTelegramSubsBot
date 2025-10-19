# app/commands.py
import time
import logging
import secrets
import string
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from app.utils.safe_exec import safe_execute

class Commands:
    def __init__(self, db, cfg, cipher):
        self.db = db
        self.cfg = cfg
        self.cipher = cipher
        self.TW_AUTH = cfg.TW_AUTH
        self.SCOPE_SETUP = ["channel:read:subscriptions"]
        self.SCOPE_USER = ["user:read:email"]

    async def send_tg(self, chat_id, text, md=False, html=False):
        """Envia mensajes a Telegram de forma segura"""
        from aiohttp import ClientSession
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if md:
            payload["parse_mode"] = "Markdown"
        elif html:
            payload["parse_mode"] = "HTML"
        try:
            async with ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    def _gen_state(self, n=32):
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    @safe_execute()
    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /setup — inicia el vínculo del streamer"""
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
        url = (f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💜 Vincular canal de Twitch", url=url)]
        ])

        await update.message.reply_text(
            "💜 <b>Vincula tu canal de Twitch</b>\n\nTras autorizar, recibirás tu enlace personalizado.",
            parse_mode="HTML", reply_markup=kb
        )

    @safe_execute()
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start — para los viewers"""
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
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at,broadcaster_slug) VALUES(%s,%s,%s,%s,%s)",
            state, user.id, "user_link", int(time.time()), b["slug"]
        )

        redirect_uri = f"{self.cfg.base_url}/twitch/callback"
        scope = quote(" ".join(self.SCOPE_USER))
        url = (f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💜 Vincular cuenta de Twitch", url=url)]
        ])
        await update.message.reply_text(
            f"💜 <b>Vincula tu Twitch para comprobar si eres sub de {slug}</b>",
            parse_mode="HTML", reply_markup=kb
        )

    @safe_execute()
    async def cmd_setgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Vincula grupo premium del streamer"""
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec(
            "UPDATE broadcasters SET group_id=%s, invite_link=%s WHERE slug=%s",
            update.effective_chat.id, link.invite_link, b["slug"]
        )

        new_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={b['dashboard_token']}"
        msg = (
            f"✅ <b>Grupo premium vinculado correctamente.</b>\n\n"
            f"📊 <b>Dashboard privado:</b>\n{new_link}"
        )
        await self.send_tg(user.id, msg, html=True)

    @safe_execute()
    async def cmd_setfreegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Vincula grupo gratuito"""
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec(
            "UPDATE broadcasters SET free_group_id=%s, free_invite_link=%s WHERE slug=%s",
            update.effective_chat.id, link.invite_link, b["slug"]
        )

        await update.message.reply_text("✅ Grupo gratuito vinculado correctamente.")

    @safe_execute()
    async def cmd_regendashlink(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Regenera el enlace del dashboard"""
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        new_token = secrets.token_urlsafe(32)
        await self.db.exec(
            "UPDATE broadcasters SET dashboard_token=%s WHERE slug=%s",
            new_token, b["slug"]
        )

        new_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={new_token}"
        msg = (
            "✅ <b>Enlace del dashboard actualizado</b>\n\n"
            "📊 Nuevo acceso privado:\n"
            f"{new_link}\n\n"
            "🛡️ El enlace anterior ha sido invalidado por seguridad."
        )
        await self.send_tg(user.id, msg, html=True)
