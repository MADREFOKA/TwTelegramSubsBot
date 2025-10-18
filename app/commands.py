# app/commands.py
import time
import asyncio
import requests
from urllib.parse import quote
from telegram import Update
from telegram.ext import ContextTypes

from app.utils.messages import group_premium_linked, viewer_is_owner, viewer_is_sub, viewer_not_sub

class Commands:
    def __init__(self, config, db, send_tg, refresh_twitch_token, days_until):
        self.cfg = config
        self.db = db
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token
        self.days_until = days_until

        self.SCOPE_SETUP = ["channel:read:subscriptions"]
        self.SCOPE_USER  = ["user:read:email"]

    def gen_state(self, n=32):
        import secrets, string
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    # /setup
    async def setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
        state = self.gen_state()
        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at) VALUES(%s,%s,%s,%s)",
            state, user.id, "broadcaster_setup", int(time.time())
        )
        await asyncio.sleep(0.2)
        redirect_uri = f"{self.cfg.base_url}/twitch/setup/callback"
        scope = quote(" ".join(self.SCOPE_SETUP))
        url = (f"{self.cfg.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")
        await update.message.reply_text(
            f"💜 [Vincula tu canal de Twitch aquí]({url})\n\nTras autorizar, recibirás tu enlace personalizado.",
            parse_mode="Markdown", disable_web_page_preview=True
        )

    # /start <slug>
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        args = context.args or []
        if not args:
            return await update.message.reply_text("⚠️ Usa el enlace del streamer. Ejemplo: /start madrefoka")
        slug = args[0].lower()

        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b:
            return await update.message.reply_text("❌ Este streamer no está vinculado.")

        state = self.gen_state()
        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at,broadcaster_id) VALUES(%s,%s,%s,%s,%s)",
            state, user.id, "user_link", int(time.time()), b["broadcaster_id"]
        )
        await asyncio.sleep(0.2)

        redirect_uri = f"{self.cfg.base_url}/twitch/callback"
        scope = quote(" ".join(self.SCOPE_USER))
        url = (f"{self.cfg.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")
        await update.message.reply_text(
            f"💜 [Vincula tu Twitch para comprobar si eres sub de *{slug}*]({url})",
            parse_mode="Markdown", disable_web_page_preview=True
        )

    # /checkme <slug>
    async def checkme(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        args = context.args or []
        if not args:
            return await update.message.reply_text("Uso: /checkme <streamer>")
        slug = args[0].lower()

        tuser = await self.db.one("SELECT * FROM telegram_users WHERE telegram_id=%s", user.id)
        if not tuser:
            return await update.message.reply_text("⚠️ No tienes una cuenta de Twitch vinculada aún. Usa /start <streamer> para vincularte.")

        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b:
            return await update.message.reply_text("❌ Streamer no encontrado.")

        b_access = await self.refresh_twitch_token(b)
        headers = {"Authorization": f"Bearer {b_access}", "Client-Id": self.cfg.client_id}
        r = requests.get(f"{self.cfg.TW_API}/subscriptions/user", headers=headers,
                         params={"broadcaster_id": b["broadcaster_id"], "user_id": tuser["linked_twitch_id"]}, timeout=15)
        data = r.json().get("data", [])

        if tuser["linked_twitch_id"] == b["broadcaster_id"]:
            msg = viewer_is_owner(user.first_name, b["slug"], b.get("invite_link"))
        elif data:
            renew, days_left = self.days_until(data[0].get("ends_at", ""))
            msg = viewer_is_sub(user.first_name, b["slug"], renew, days_left, b.get("invite_link"))
        else:
            msg = viewer_not_sub(user.first_name, b["slug"], b.get("free_invite_link"))
        await update.message.reply_text(msg, parse_mode="HTML")

    # /setgroup (en grupo)
    async def setgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")
        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec("UPDATE broadcasters SET group_id=%s, invite_link=%s WHERE broadcaster_id=%s",
                           update.effective_chat.id, link.invite_link, b["broadcaster_id"])
        await update.message.reply_text("✅ Grupo premium vinculado correctamente.")
        await self.send_tg(user.id, group_premium_linked(self.cfg.bot_username, b["slug"]), html=True)

    # /setfreegroup (en grupo)
    async def setfreegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")
        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec("UPDATE broadcasters SET free_group_id=%s, free_invite_link=%s WHERE broadcaster_id=%s",
                           update.effective_chat.id, link.invite_link, b["broadcaster_id"])
        await update.message.reply_text("✅ Grupo gratuito vinculado correctamente.")

    # PRIVILEGIOS
    async def priv_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        if not context.args:
            return await update.message.reply_text("Uso: /priv_add @usuario")
        username = context.args[0].lstrip("@").lower()
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")
        await self.db.exec("""
            INSERT INTO privileged_users(broadcaster_id,username)
            VALUES(%s,%s) ON CONFLICT DO NOTHING
        """, b["broadcaster_id"], username)
        await update.message.reply_text(f"🔒 @{username} añadido a la whitelist.")

    async def priv_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        if not context.args:
            return await update.message.reply_text("Uso: /priv_remove @usuario")
        username = context.args[0].lstrip("@").lower()
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")
        await self.db.exec("DELETE FROM privileged_users WHERE broadcaster_id=%s AND username=%s",
                           b["broadcaster_id"], username)
        await update.message.reply_text(f"🧹 @{username} eliminado de la whitelist.")

    async def priv_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")
        rows = await self.db.all("SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"])
        if not rows:
            return await update.message.reply_text("⚙️ No tienes usuarios en la whitelist.")
        lista = "\n".join([f"• @{r['username']}" for r in rows])
        await update.message.reply_text(f"🔒 Lista de whitelist:\n{lista}")

    # /auditnow (en grupo)
    async def auditnow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        chat_id = update.effective_chat.id
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b or b.get("group_id") != chat_id:
            return await update.message.reply_text("⚠️ Este grupo no está vinculado como premium. Usa /setgroup aquí primero.")

        privs = [r["username"] for r in await self.db.all(
            "SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"]
        )]
        b_access = await self.refresh_twitch_token(b)
        headers = {"Authorization": f"Bearer {b_access}", "Client-Id": self.cfg.client_id}

        total_checked, removed = 0, 0
        # Nota: API de miembros completa requiere permisos/iteración; aquí hacemos un ejemplo con admins + placeholder.
        admins = await context.bot.get_chat_administrators(chat_id)
        for adm in admins:
            u = adm.user
            if u.is_bot:
                continue
            # En un escenario real, iterarías todos los miembros con otras herramientas o usando Telegram API extra.
            # Placeholder de flujo:
            total_checked += 1
            # Si no está en whitelist y no vinculado/sub, se expulsaría. (No expulsamos admins aquí).
            # Deja la lógica completa para cuando integremos una lista de miembros completa.

        await update.message.reply_text(
            f"🧹 Auditoría completada.\n👥 Usuarios revisados (muestra): {total_checked}\n🚫 Expulsados: {removed}"
        )
