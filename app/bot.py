import os, time, logging, asyncio, secrets, string, requests
from urllib.parse import quote
from datetime import datetime, timezone
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from .db import Database

# -------------------------------
# CONFIGURACIÓN GLOBAL
# -------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

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

    # -------------------------------
    # UTILIDADES
    # -------------------------------
    def gen_state(self, n=32):
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    async def send_tg(self, chat_id, text, md=False):
        try:
            payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            if md:
                payload["parse_mode"] = "Markdown"
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logging.warning(f"Error enviando mensaje Telegram: {e}")

    async def refresh_twitch_token(self, broadcaster):
        """Refresca tokens vencidos automáticamente"""
        if not broadcaster.get("refresh_token"):
            return broadcaster["access_token"]
        elapsed = time.time() - broadcaster["token_obtained_at"]
        if elapsed < max(0, broadcaster["token_expires_in"] - 86400):
            return broadcaster["access_token"]
        try:
            data = requests.post(self.TW_TOKEN, data={
                "grant_type": "refresh_token",
                "refresh_token": broadcaster["refresh_token"],
                "client_id": self.client_id,
                "client_secret": self.client_secret
            }, timeout=15).json()
            if "access_token" in data:
                await self.db.exec("""
                    UPDATE broadcasters
                    SET access_token=%s, refresh_token=%s, token_obtained_at=%s, token_expires_in=%s
                    WHERE broadcaster_id=%s
                """, data["access_token"], data.get("refresh_token", broadcaster["refresh_token"]),
                     int(time.time()), data.get("expires_in", broadcaster["token_expires_in"]),
                     broadcaster["broadcaster_id"])
                return data["access_token"]
        except Exception as e:
            logging.warning(f"Error refrescando token: {e}")
        return broadcaster["access_token"]

    # -------------------------------
    # /SETUP — STREAMER
    # -------------------------------
    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
        state = self.gen_state()
        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at) VALUES(%s,%s,%s,%s)",
            state, user.id, "broadcaster_setup", int(time.time())
        )
        redirect_uri = f"{self.base_url}/twitch/setup/callback"
        scope = quote(" ".join(self.SCOPE_SETUP))
        url = (f"{self.TW_AUTH}?client_id={self.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")
        await update.message.reply_text(
            f"💜 [Vincula tu canal de Twitch aquí]({url})\n\nTras autorizar, recibirás tu enlace personalizado.",
            parse_mode="Markdown", disable_web_page_preview=True
        )

    # -------------------------------
    # /START — VIEWER
    # -------------------------------
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
        state = self.gen_state()
        await self.db.exec("INSERT INTO oauth_states(state,telegram_id,purpose,created_at,broadcaster_id) VALUES(%s,%s,%s,%s,%s)",
                           state, user.id, "user_link", int(time.time()), b["broadcaster_id"])
        redirect_uri = f"{self.base_url}/twitch/callback"
        scope = quote(" ".join(self.SCOPE_USER))
        url = (f"{self.TW_AUTH}?client_id={self.client_id}&redirect_uri={quote(redirect_uri)}"
               f"&response_type=code&scope={scope}&state={state}&force_verify=true")
        await update.message.reply_text(
            f"💜 [Vincula tu Twitch para comprobar si eres sub de *{slug}*]({url})",
            parse_mode="Markdown", disable_web_page_preview=True
        )

    # -------------------------------
    # /CHECKME — VIEWER
    # -------------------------------
    async def cmd_checkme(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        args = context.args if context.args else []
        if not args:
            return await update.message.reply_text("Uso: /checkme <streamer>")
        slug = args[0].lower()
        tuser = await self.db.one("SELECT * FROM telegram_users WHERE telegram_id=%s", user.id)
        if not tuser:
            return await update.message.reply_text("⚠️ No tienes una cuenta de Twitch vinculada aún.")
        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b:
            return await update.message.reply_text("❌ Streamer no encontrado.")
        b["access_token"] = await self.refresh_twitch_token(b)
        headers = {"Authorization": f"Bearer {b['access_token']}", "Client-Id": self.client_id}
        r = requests.get(f"{self.TW_API}/subscriptions/user", headers=headers,
                         params={"broadcaster_id": b["broadcaster_id"], "user_id": tuser["linked_twitch_id"]}, timeout=15)
        data = r.json().get("data")
        if r.status_code == 200 and data:
            msg = f"💜 Sigues siendo sub de *{b['slug']}* 🎟️"
            if b.get("invite_link"):
                msg += f"\nAccede al grupo premium:\n{b['invite_link']}"
        else:
            msg = f"😔 No eres sub de *{b['slug']}*."
            if b.get("free_invite_link"):
                msg += f"\nGrupo gratis:\n{b['free_invite_link']}"
        await update.message.reply_text(msg, parse_mode="Markdown")

    # -------------------------------
    # /SETGROUP — STREAMER
    # -------------------------------
    async def cmd_setgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await self.send_tg(user.id, f"💜 Grupo premium vinculado.\n🔗 Enlace para viewers:\nhttps://t.me/{self.bot_username}?start={b['slug']}")

    # -------------------------------
    # /SETFREEGROUP — STREAMER
    # -------------------------------
    async def cmd_setfreegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # -------------------------------
    # /AUDITFULL — STREAMER
    # -------------------------------
    async def cmd_audit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        chat_id = update.effective_chat.id
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b or b.get("group_id") != chat_id:
            return await update.message.reply_text("⚠️ Este grupo no está vinculado como premium.")
        privs = [r["username"] for r in await self.db.all("SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"])]
        members = await context.bot.get_chat_administrators(chat_id)
        count = 0
        for m in members:
            if m.user.username and m.user.username.lower() in privs:
                continue
            count += 1
        await update.message.reply_text(f"🧹 Auditoría completada. Usuarios revisados: {count}")

    # -------------------------------
    # PRIVILEGIOS
    # -------------------------------
    async def cmd_priv_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        if not context.args:
            return await update.message.reply_text("Uso: /privilege_add @usuario")
        username = context.args[0].lstrip("@").lower()
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes canal vinculado.")
        await self.db.exec("INSERT INTO privileged_users(broadcaster_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                           b["broadcaster_id"], username)
        await update.message.reply_text(f"🔒 @{username} añadido como privilegiado.")

    async def cmd_priv_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        if not context.args:
            return await update.message.reply_text("Uso: /privilege_remove @usuario")
        username = context.args[0].lstrip("@").lower()
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes canal vinculado.")
        await self.db.exec("DELETE FROM privileged_users WHERE broadcaster_id=%s AND username=%s",
                           b["broadcaster_id"], username)
        await update.message.reply_text(f"🧹 @{username} eliminado de privilegios.")

    async def cmd_priv_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes canal vinculado.")
        rows = await self.db.all("SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"])
        if not rows:
            return await update.message.reply_text("⚙️ Sin usuarios privilegiados.")
        lista = "\n".join([f"• @{r['username']}" for r in rows])
        await update.message.reply_text(f"🔒 Lista de usuarios privilegiados:\n{lista}")

    # -------------------------------
    # CALLBACKS TWITCH (setup + viewer)
    # -------------------------------
    async def twitch_callback_setup(self, request: web.Request):
        """Callback OAuth cuando un streamer hace /setup"""
        try:
            params = request.rel_url.query
            code, state = params.get("code"), params.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta código o estado.", status=400)
    
            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='broadcaster_setup'",
                state
            )
            if not row:
                return web.Response(text="❌ Estado no válido o expirado.", status=400)
    
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{self.base_url}/twitch/setup/callback"
            }
    
            data = requests.post(self.TW_TOKEN, data=payload, timeout=15).json()
            if "access_token" not in data:
                return web.Response(text=f"❌ Error al obtener token: {data}", status=400)
    
            access_token = data["access_token"]
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 0)
            token_time = int(time.time())
    
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": self.client_id
            }
            me = requests.get(f"{self.TW_API}/users", headers=headers, timeout=15).json()["data"][0]
            slug = me["login"].lower()
    
            await self.db.exec("""
                INSERT INTO broadcasters(broadcaster_id, owner_telegram_id, access_token, refresh_token,
                                         token_obtained_at, token_expires_in, slug)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (broadcaster_id)
                DO UPDATE SET access_token=EXCLUDED.access_token,
                              refresh_token=EXCLUDED.refresh_token,
                              token_obtained_at=EXCLUDED.token_obtained_at,
                              token_expires_in=EXCLUDED.token_expires_in,
                              slug=EXCLUDED.slug
            """, me["id"], row["telegram_id"], access_token, refresh_token, token_time, expires_in, slug)
    
            bot_link = f"https://t.me/{self.bot_username}?start={slug}"
            msg = (
                f"✅ *Canal vinculado correctamente.*\n\n"
                f"🎥 Canal: *{me['display_name']}*\n"
                f"🔗 Enlace para tus viewers:\n{bot_link}\n\n"
                f"Para usar el bot en tus grupos:\n"
                f"• Usa /setgroup en tu grupo premium.\n"
                f"• Usa /setfreegroup en tu grupo gratuito.\n\n"
                f"Comandos de gestión:\n"
                f"🧹 /auditfull — limpia no-suscriptores\n"
                f"⭐ /privilege_add /privilege_remove /privilege_list — gestiona excepciones VIP."
            )
            await self.send_tg(row["telegram_id"], msg, markdown=True)
    
            # 🔁 Redirige de vuelta al bot de Telegram (sin mostrar página)
            raise web.HTTPFound(location=f"tg://resolve?domain={self.bot_username}")
    
        except Exception as e:
            logging.exception("Error en twitch_callback_setup:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)

    async def twitch_callback_user(self, request: web.Request):
        """Callback OAuth cuando un viewer se vincula"""
        try:
            params = request.rel_url.query
            code, state = params.get("code"), params.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta código o estado.", status=400)
    
            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='user_link'", state
            )
            if not row:
                return web.Response(text="❌ Estado no válido o expirado.", status=400)
    
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.base_url}/twitch/callback"
            }
    
            data = requests.post(self.TW_TOKEN, data=payload, timeout=15).json()
            if "access_token" not in data:
                return web.Response(text=f"❌ Error al obtener token: {data}", status=400)
    
            access_token = data["access_token"]
            headers_user = {"Authorization": f"Bearer {access_token}", "Client-Id": self.client_id}
            me = requests.get(f"{self.TW_API}/users", headers=headers_user, timeout=15).json()["data"][0]
    
            # Guarda o actualiza al usuario en BD
            await self.db.exec("""
                INSERT INTO telegram_users(telegram_id, username, linked_twitch_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id)
                DO UPDATE SET username=EXCLUDED.username, linked_twitch_id=EXCLUDED.linked_twitch_id
            """, row["telegram_id"], me["login"], me["id"])
    
            b = await self.db.one("SELECT * FROM broadcasters WHERE broadcaster_id=%s", row["broadcaster_id"])
            if not b:
                return web.Response(text="❌ Canal no encontrado.", status=400)
    
            b["access_token"] = await self.refresh_twitch_token(b)
            headers_b = {"Authorization": f"Bearer {b['access_token']}", "Client-Id": self.client_id}
    
            # Comprueba si el usuario es sub o el propietario del canal
            r = requests.get(
                f"{self.TW_API}/subscriptions/user",
                headers=headers_b,
                params={"broadcaster_id": b["broadcaster_id"], "user_id": me["id"]},
                timeout=15
            )
            data_subs = r.json().get("data")
    
            if r.status_code == 200 and data_subs:
                msg = (
                    f"💜 *{me['display_name']}*, eres sub de *{b['slug']}* 🎟️\n\n"
                    f"🔗 Grupo premium:\n{b.get('invite_link', '—')}"
                )
            elif me["id"] == b["broadcaster_id"]:
                msg = (
                    f"👑 *{me['display_name']}*, eres el propietario de *{b['slug']}* 🎟️\n\n"
                    f"Acceso automático al grupo premium:\n{b.get('invite_link', '—')}"
                )
            else:
                msg = (
                    f"😔 *{me['display_name']}*, no apareces como sub de *{b['slug']}*.\n\n"
                    f"💬 Puedes unirte al grupo gratuito aquí:\n{b.get('free_invite_link', '—')}"
                )
    
            await self.send_tg(row["telegram_id"], msg, markdown=True)
    
            raise web.HTTPFound(location=f"tg://resolve?domain={self.bot_username}")
    
        except Exception as e:
            logging.exception("Error en twitch_callback_user:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)
            
    # -------------------------------
    # INICIO Y WEBHOOKS
    # -------------------------------
    async def webhook_handler(self, request: web.Request):
        data = await request.json()
        update = Update.de_json(data, self.app.bot)
        await self.app.process_update(update)
        return web.Response(text="OK")

    async def start(self):
        await self.db.init()
        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("setup", self.cmd_setup))
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("checkme", self.cmd_checkme))
        app.add_handler(CommandHandler("setgroup", self.cmd_setgroup))
        app.add_handler(CommandHandler("setfreegroup", self.cmd_setfreegroup))
        app.add_handler(CommandHandler("auditfull", self.cmd_audit))
        app.add_handler(CommandHandler("privilege_add", self.cmd_priv_add))
        app.add_handler(CommandHandler("privilege_remove", self.cmd_priv_remove))
        app.add_handler(CommandHandler("privilege_list", self.cmd_priv_list))
        self.app = app

        webhook_url = f"{self.base_url}/webhook"
        requests.get(f"https://api.telegram.org/bot{self.token}/setWebhook", params={"url": webhook_url}, timeout=10)

        web_app = web.Application()
        web_app["bot"] = app
        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/twitch/setup/callback", self.twitch_callback_setup)
        web_app.router.add_get("/twitch/callback", self.twitch_callback_user)
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo correctamente."))

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()

        await app.initialize()
        await app.start()
        logging.info("✅ Bot y servidor web iniciados correctamente.")
        await asyncio.Event().wait()
