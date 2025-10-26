import time
import logging
import secrets
import string
from urllib.parse import quote
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from app.utils.safe_exec import safe_exec
from app.utils.messages import guide_message

class Commands:
    def __init__(self, db, cfg, cipher):
        self.db = db
        self.cfg = cfg
        self.cipher = cipher
        self.TW_API = cfg.TW_API
        self.TW_AUTH = cfg.TW_AUTH
        self.TW_TOKEN = cfg.TW_TOKEN
        self.SCOPE_SETUP = ["channel:read:subscriptions"]
        self.SCOPE_USER = ["user:read:subscriptions", "user:read:email"]
        
    # ───────────────────────────────
    # 🧩 UTILIDAD
    # ───────────────────────────────
    async def send_tg(self, chat_id, text, md=False, html=False):
        import aiohttp
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if md: payload["parse_mode"] = "Markdown"
        elif html: payload["parse_mode"] = "HTML"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    # ───────────────────────────────
    # ⚙️ /SETUP — STREAMER
    # ───────────────────────────────
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
            f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code&scope={scope}&state={state}&force_verify=true"
        )

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💜 Vincular canal de Twitch", url=url)]])
        await update.message.reply_text(
            "Pulsa el botón para vincular tu canal de Twitch con el bot:",
            reply_markup=keyboard
        )

    # ───────────────────────────────
    # 🚀 /START — VIEWER
    # ───────────────────────────────
    @safe_exec()
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
    
        # 🧾 Guardar datos básicos de Telegram en telegram_users
        try:
            await self.db.exec("""
                INSERT INTO telegram_users (telegram_id, username, first_name, last_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name;
            """, user.id, user.username, user.first_name, user.last_name)
            logging.info(f"💾 Datos Telegram actualizados: {user.first_name} {user.last_name} (@{user.username})")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo guardar datos de Telegram: {e}")
    
        # 📡 Continuar con la vinculación normal
        args = context.args if context.args else []
        if not args:
            return await update.message.reply_text(
                "⚠️ Usa el enlace del streamer. Ejemplo: /start *nickdelstreamer* "
            )
    
        # 👇 El argumento puede ser slug o display_name
        arg = args[0]
    
        # Buscar broadcaster por id, slug o display_name
        b = await self.db.one(
            """
            SELECT * FROM broadcasters
            WHERE broadcaster_id=%s OR LOWER(slug)=%s OR LOWER(display_name)=LOWER(%s)
            """,
            arg, arg.lower(), arg
        )
    
        if not b:
            return await update.message.reply_text("❌ Este streamer no está vinculado.")
    
        # Generar estado OAuth
        state = self._gen_state()
        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at,broadcaster_id) VALUES(%s,%s,%s,%s,%s)",
            state, user.id, "user_link", int(time.time()), b["broadcaster_id"]
        )
    
        redirect_uri = f"{self.cfg.base_url}/twitch/callback"
        scope = quote(" ".join(self.SCOPE_USER))
        url = (
            f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code&scope={scope}&state={state}&force_verify=true"
        )
    
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💜 Verificar suscripción", url=url)]])
        await update.message.reply_text(
            f"Pulsa el botón para vincular tu Twitch y comprobar si eres sub de {b['display_name']}:\n\n"
            f"⚠️ Si tienes 2FA o autenticación en Twitch, usa un PC para completar la vinculación.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
    # ───────────────────────────────
    # 🏠 /SETGROUP — GRUPO PREMIUM
    # ───────────────────────────────
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
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Abrir Dashboard", url=new_link)]])
        try:
            await update.message.delete()
        except:
            pass
        await self.send_tg(b["owner_telegram_id"], f"✅ Grupo premium vinculado correctamente.\n\n💜 Enlace para viewers:\nhttps://t.me/{self.cfg.bot_username}?start={b['slug']}",)

    # ───────────────────────────────
    # 🎁 /SETFREEGROUP — GRUPO GRATUITO
    # ───────────────────────────────
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
        try:
            await update.message.delete()
        except:
            pass
        await self.send_tg(b["owner_telegram_id"], f"✅ Grupo gratuito vinculado correctamente.")

    # ───────────────────────────────
    # 🔐 /REGENDASHLINK
    # ───────────────────────────────
    @safe_exec()
    async def cmd_regendashlink(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

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
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Abrir nuevo Dashboard", url=new_link)]])
        await update.message.reply_text(
            "✅ Nuevo enlace del Dashboard generado.\n🛡️ El anterior ha sido invalidado.",
            reply_markup=keyboard
        )

    # ───────────────────────────────
    # 🧬 AUXILIAR
    # ───────────────────────────────
    def _gen_state(self, n=32):
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    # ───────────────────────────────
    # 🧬 GUIA DE USO
    # ───────────────────────────────
    async def on_callback(self, update, context):
        """Responde a botones inline con callback_data."""
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
    
        if query.data == "guide_usage":
            await query.message.reply_html(
                guide_message(),
                disable_web_page_preview=True
            )
    # ───────────────────────────────
    # 👑 PRIVILEGIOS — Añadir / Eliminar / Listar
    # ───────────────────────────────
    @safe_exec()
    async def cmd_priv_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Añade un usuario como privilegiado respondiendo a su mensaje."""
        if not update.message or not update.effective_user:
            return
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", update.effective_user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes permiso para usar este comando.")

        if not update.message.reply_to_message:
            return await update.message.reply_text("👉 Usa este comando respondiendo al usuario que quieras añadir.")
        target = update.message.reply_to_message.from_user

        await self.db.add_privileged_user(b["slug"], b["broadcaster_id"], target.id, target.username, update.effective_user.id)
        await update.message.reply_text(f"✅ {target.username or target.first_name} añadido como usuario privilegiado.")

    @safe_exec()
    async def cmd_priv_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Elimina a un usuario privilegiado."""
        if not update.message or not update.effective_user:
            return
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", update.effective_user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes permiso para usar este comando.")
        if not update.message.reply_to_message:
            return await update.message.reply_text("👉 Usa este comando respondiendo al usuario que quieras eliminar.")
        target = update.message.reply_to_message.from_user

        await self.db.remove_privileged_user(b["slug"], b["broadcaster_id"], target.id)
        await update.message.reply_text(f"🗑️ {target.username or target.first_name} eliminado de privilegiados.")

    @safe_exec()
    async def cmd_priv_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Lista todos los usuarios privilegiados del streamer."""
        if not update.message or not update.effective_user:
            return
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", update.effective_user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes permiso para usar este comando.")

        rows = await self.db.list_privileged_users(b["slug"])
        if not rows:
            return await update.message.reply_text("📭 No hay usuarios privilegiados actualmente.")
        text = "👑 *Usuarios privilegiados:*\n\n" + "\n".join(
            [f"• {r['username']} (ID: `{r['telegram_id']}`)" for r in rows]
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        
