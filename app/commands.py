# app/commands.py
import time
import logging
import requests
import asyncio
from urllib.parse import quote
from telegram import Update
from telegram.ext import ContextTypes

from app.utils.safe_exec import safe_execute  # ✅ nuevo decorador de seguridad


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

    # ---------------------------
    # UTILIDAD PARA ENVIAR MENSAJES
    # ---------------------------
    async def send_tg(self, chat_id, text, md=False, html=False):
        """Envia mensajes a Telegram de forma segura"""
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if md:
            payload["parse_mode"] = "Markdown"
        elif html:
            payload["parse_mode"] = "HTML"

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    # ---------------------------
    # /SETUP — STREAMER
    # ---------------------------
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
        
        new_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={b['dashboard_token']}"
        msg = (
            "✅ <b>Grupo premium vinculado correctamente.</b>\n\n"
            f"💜 Enlace viewers:\nhttps://t.me/{self.cfg.bot_username}?start={b['slug']}\n\n"
            f"📊 <b>Dashboard privado:</b>\n{new_link}"
        )
        await self.send_tg(user.id, msg, html=True)

        redirect_uri = f"{self.cfg.base_url}/twitch/setup/callback"
        scope = quote(" ".join(self.SCOPE_SETUP))
        url = (f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")

        await update.message.reply_text(
            f"💜 [Vincula tu canal de Twitch aquí]({url})\n\n"
            f"Tras autorizar, recibirás tu enlace personalizado.",
            parse_mode="Markdown", disable_web_page_preview=True
        )

    # ---------------------------
    # /START — VIEWER
    # ---------------------------
    @safe_execute()
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start — para los viewers que se vinculan"""
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
        url = (f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")

        await update.message.reply_text(
            f"💜 [Vincula tu Twitch para comprobar si eres sub de *{slug}*]({url})",
            parse_mode="Markdown", disable_web_page_preview=True
        )

    # ---------------------------
    # /SETGROUP — STREAMER
    # ---------------------------
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
            "UPDATE broadcasters SET group_id=%s, invite_link=%s WHERE broadcaster_id=%s",
            update.effective_chat.id, link.invite_link, b["broadcaster_id"]
        )

        await update.message.reply_text("✅ Grupo premium vinculado correctamente.")
        await self.send_tg(user.id,
            f"💜 Grupo premium vinculado.\n🔗 Enlace viewers:\nhttps://t.me/{self.cfg.bot_username}?start={b['slug']}")

    # ---------------------------
    # /SETFREEGROUP — STREAMER
    # ---------------------------
    @safe_execute()
    async def cmd_setfreegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Vincula grupo gratuito del streamer"""
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
    # /REGENDASHLINK — STREAMER
    # ---------------------------
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
        msg = (
            "✅ <b>Enlace del dashboard actualizado</b>\n\n"
            "📊 Nuevo acceso privado:\n"
            f"{new_link}\n\n"
            "🛡️ El enlace anterior ha sido invalidado por seguridad."
        )
        await self.send_tg(user.id, msg, html=True)

    # ---------------------------
    # AUXILIAR
    # ---------------------------
    def _gen_state(self, n=32):
        import secrets, string
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))
