# app/bot.py
import os
import time
import logging
import asyncio
import requests
import aiohttp
import aiohttp_jinja2
import jinja2
from datetime import datetime, timezone
from aiohttp import web
from telegram.ext import (ApplicationBuilder, CommandHandler, ChatMemberHandler, CallbackQueryHandler)
from telegram import Update, ChatMember
from app.db import Database
from app.security import TokenCipher
from app.commands import Commands
from app.oauth import OAuthHandlers
from app.auditor import Auditor
from app.dashboard import Dashboard


class Config:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.client_id = os.getenv("OAUTH_CLIENT_ID")
        self.client_secret = os.getenv("OAUTH_CLIENT_SECRET")
        self.base_url = os.getenv("BASE_URL")
        self.bot_username = os.getenv("BOT_USERNAME") or "TwSubCheck_bot"
        self.secret_token = os.getenv("FLASK_SECRET")
        self.TW_API = "https://api.twitch.tv/helix"
        self.TW_AUTH = "https://id.twitch.tv/oauth2/authorize"
        self.TW_TOKEN = "https://id.twitch.tv/oauth2/token"
        self.bot_owner_id = int(os.getenv("BOT_OWNER_ID", 0))        

class TwitchSubBot:
    def __init__(self, db: Database):
        self.db = db
        self.cfg = Config()
        self.cipher = TokenCipher()
        self.app = None

        # Componentes
        self.cmds = Commands(self.db, self.cfg, self.cipher)
        self.oauth = OAuthHandlers(
            self.cfg, self.db, self.send_tg, self.refresh_twitch_token, self.days_until, self.cipher
        )
        self.auditor = Auditor(self.db, self.cfg, self.send_tg, self.refresh_twitch_token)
        self.dashboard = Dashboard(self.db, self.cfg, self.refresh_twitch_token)

    async def send_tg(self, chat_id, text, md=False, html=False, disable_preview=False, reply_markup=None):
        """Envia mensajes a Telegram de forma segura."""
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
            "disable_notification": False,
        }

        if md:
            payload["parse_mode"] = "Markdown"
        elif html:
            payload["parse_mode"] = "HTML"

        if reply_markup:
            payload["reply_markup"] = reply_markup.to_dict()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    async def refresh_twitch_token(self, broadcaster_row: dict) -> str:
        """Renueva tokens de Twitch si han caducado. Con diagnóstico y aviso automático."""
        access_token = self.cipher.decrypt(broadcaster_row["access_token"])
        refresh_token = self.cipher.decrypt(broadcaster_row.get("refresh_token"))
        broadcaster_id = broadcaster_row["broadcaster_id"]
        owner_tg_id = broadcaster_row["owner_telegram_id"]
    
        obtained = broadcaster_row.get("token_obtained_at") or 0
        expires = broadcaster_row.get("token_expires_in") or 0
    
        # 🔥 FIX: obtained en el futuro => no refresca nunca => 401 masivo
        if obtained > time.time():
            logging.warning(
                f"⚠️ token_obtained_at en el futuro para broadcaster_id={broadcaster_id} "
                f"(obtained={obtained}, now={int(time.time())}). Forzando refresh."
            )
            obtained = 0
    
        # Si falta refresh_token → no se puede renovar nunca
        if not refresh_token:
            logging.warning(f"⚠️ Broadcaster {broadcaster_id} sin refresh_token. Usando access_token actual.")
            return access_token
    
        # Si aún no ha caducado → devolver
        if time.time() - obtained < max(0, expires - 60):
            return access_token
    
        logging.info(f"🔄 Renovando token de Twitch para broadcaster_id={broadcaster_id}...")
    
        try:
            data = requests.post(
                self.cfg.TW_TOKEN,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.cfg.client_id,
                    "client_secret": self.cfg.client_secret
                },
                timeout=15
            ).json()
    
            # ⚠ Diagnóstico: error explícito
            if "access_token" not in data:
                error_msg = data.get("message") or data.get("error") or "Error desconocido"
                logging.error(f"❌ ERROR al refrescar el token de Twitch: {data}")
    
                # Enviar aviso al streamer SOLO una vez cada 24 horas
                await self.send_tg(
                    owner_tg_id,
                    f"⚠️ *Error renovando permisos de Twitch*\n\n"
                    f"Tu token ha caducado o fue revocado.\n"
                    f"Motivo: `{error_msg}`\n\n"
                    "Por favor ejecuta /setup de nuevo para reactivar el bot.",
                    md=True
                )
    
                return access_token  # devolvemos el token viejo para no romper el flujo
    
            # ✔ Token renovado correctamente
            access_token = data["access_token"]
            new_refresh_token = data.get("refresh_token", refresh_token)
    
            enc_access = self.cipher.encrypt(access_token)
            enc_refresh = self.cipher.encrypt(new_refresh_token)
    
            await self.db.exec("""
                UPDATE broadcasters
                SET access_token=%s,
                    refresh_token=%s,
                    token_obtained_at=%s,
                    token_expires_in=%s
                WHERE broadcaster_id=%s
            """, enc_access, enc_refresh, int(time.time()), data.get("expires_in", expires), broadcaster_id)
    
            logging.info(f"✅ Token renovado correctamente para broadcaster_id={broadcaster_id}")
    
        except Exception as e:
            logging.exception(f"❌ EXCEPCIÓN renovando token de Twitch: {e}")
    
            await self.send_tg(
                owner_tg_id,
                "⚠️ *Fallo al contactar con Twitch para renovar el token*\n"
                "Por favor ejecuta /setup de nuevo para reactivar los permisos.",
                md=True
            )
        return access_token

    def days_until(self, iso_ts: str):
        try:
            renew_time = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            diff = renew_time - now
            days = max(diff.days, 0)
            return renew_time.strftime("%d/%m/%Y %H:%M UTC"), days
        except Exception:
            return "desconocida", 0

    async def webhook_handler(self, request: web.Request):
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if token and token != self.cfg.secret_token:
            return web.Response(text="Unauthorized", status=403)
        try:
            body = await request.json()
            update = Update.de_json(body, self.app.bot)
            await self.app.update_queue.put(update)
            return web.Response(text="OK")
        except Exception as e:
            logging.exception(f"Error webhook: {e}")
            return web.Response(text="Error", status=500)

    async def on_member_update(self, update: Update, context):
        """
        Detecta cambios de estado de miembros.
        - Entrada: Verifica si está en telegram_users como sub antes de permitir
        - Salida: Limpia de group_members
        """
        member = update.chat_member
        new_status = member.new_chat_member.status
        old_status = member.old_chat_member.status
        tg_user = member.new_chat_member.user
        chat = update.effective_chat

        # Ignorar bots
        if tg_user.is_bot:
            return

        try:
            # Buscar broadcaster asociado a este grupo
            b = await self.db.one("""
                SELECT * FROM broadcasters 
                WHERE group_id=%s OR free_group_id=%s
            """, chat.id, chat.id)

            if not b:
                logging.info(f"ℹ️ Grupo {chat.id} no está vinculado a ningún broadcaster")
                return

            is_premium_group = (chat.id == b.get("group_id"))
            is_free_group = (chat.id == b.get("free_group_id"))

            # ═══════════════════════════════════════
            # 📥 USUARIO SE UNIÓ AL GRUPO
            # ═══════════════════════════════════════
            if new_status in ("member", "administrator", "creator") and old_status in ("left", "kicked"):
                
                # 🔥 GRUPO PREMIUM: Verificar que esté autorizado
                if is_premium_group:
                    # Verificar si está registrado como sub
                    tu = await self.db.one("""
                        SELECT * FROM telegram_users
                        WHERE telegram_id=%s AND streamer_id=%s AND is_subscribed=TRUE
                    """, tg_user.id, b["broadcaster_id"])

                    if not tu:
                        # NO está autorizado → expulsar inmediatamente
                        logging.warning(
                            f"🚫 Usuario {tg_user.username or tg_user.id} entró al grupo premium "
                            f"sin verificar suscripción. Expulsando..."
                        )
                        try:
                            await context.bot.ban_chat_member(chat.id, tg_user.id)
                            await asyncio.sleep(0.5)
                            await context.bot.unban_chat_member(chat.id, tg_user.id)
                        except Exception as e:
                            logging.error(f"❌ Error expulsando usuario no autorizado: {e}")
                        return

                    # SÍ está autorizado → registrar en group_members
                    await self.db.exec("""
                        INSERT INTO group_members (
                            group_id, telegram_id, username, twitch_id, twitch_login,
                            broadcaster_id, streamer_slug, is_subscribed, joined_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, NOW())
                        ON CONFLICT (group_id, telegram_id)
                        DO UPDATE SET
                            username = EXCLUDED.username,
                            twitch_id = EXCLUDED.twitch_id,
                            twitch_login = EXCLUDED.twitch_login,
                            is_subscribed = TRUE
                    """, chat.id, tg_user.id, tg_user.username, 
                         tu.get("linked_twitch_id"), tu.get("twitch_login"),
                         b["broadcaster_id"], b["slug"])
                    
                    logging.info(f"✅ Usuario autorizado añadido: @{tg_user.username} → {b['slug']}")

                # 🎁 GRUPO GRATUITO: Solo registrar
                elif is_free_group:
                    logging.info(f"ℹ️ Usuario {tg_user.username} se unió al grupo gratuito")

            # ═══════════════════════════════════════
            # 📤 USUARIO SALIÓ/FUE EXPULSADO
            # ═══════════════════════════════════════
            elif new_status in ("left", "kicked"):
                if is_premium_group:
                    await self.db.exec("""
                        DELETE FROM group_members 
                        WHERE group_id=%s AND telegram_id=%s
                    """, chat.id, tg_user.id)
                    logging.info(f"🚪 Usuario eliminado de group_members: @{tg_user.username}")

        except Exception as e:
            logging.exception(f"❌ Error en on_member_update: {e}")

    # ────────────────────────────────────────────────
    # 🔥 START PRINCIPAL - CORREGIDO
    # ────────────────────────────────────────────────
    async def start(self):
        port = int(os.getenv("PORT", "8080"))
        
        # ════════════════════════════════════════════════════════════
        # 🔥 PASO 1: INICIALIZAR BD Y BOT
        # ════════════════════════════════════════════════════════════
        await self.db.init()
        logging.info("✅ Base de datos inicializada")
        
        app = ApplicationBuilder().token(self.cfg.token).build()
        self.app = app

        # Comandos
        app.add_handler(CommandHandler("setup", self.cmds.cmd_setup))
        app.add_handler(CommandHandler("start", self.cmds.cmd_start))
        app.add_handler(CommandHandler("setgroup", self.cmds.cmd_setgroup))
        app.add_handler(CommandHandler("setfreegroup", self.cmds.cmd_setfreegroup))
        app.add_handler(CommandHandler("regendashlink", self.cmds.cmd_regendashlink))
        app.add_handler(CommandHandler("auditnow", self.auditor.cmd_auditnow))
        # /limpieza antes expulsaba según telegram_users.is_subscribed (dato guardado,
        # desactualizado tras renovaciones). Ahora usa la auditoría segura contra Twitch.
        app.add_handler(CommandHandler("limpieza", self.auditor.cmd_auditnow))
        app.add_handler(CommandHandler("priv_add", self.cmds.cmd_priv_add))
        app.add_handler(CommandHandler("priv_delete", self.cmds.cmd_priv_delete))
        app.add_handler(CommandHandler("priv_list", self.cmds.cmd_priv_list))
        app.add_handler(CallbackQueryHandler(self.cmds.on_callback, pattern="^guide_usage$"))
        app.add_handler(CommandHandler("sync_members", self.cmds.cmd_sync_members))
        app.add_handler(CommandHandler("sync_group", self.cmds.cmd_sync_group))
        app.add_handler(CommandHandler("auditcheck", self.auditor.cmd_auditcheck))
        app.add_handler(CommandHandler("verify", self.auditor.cmd_verify))
        app.add_handler(CommandHandler("checksub", self.auditor.cmd_checksub))
        app.add_handler(ChatMemberHandler(self.on_member_update, ChatMemberHandler.CHAT_MEMBER))

        # ════════════════════════════════════════════════════════════
        # 🔥 PASO 2: CONFIGURAR WEBHOOK
        # ════════════════════════════════════════════════════════════
        webhook_url = f"{self.cfg.base_url}/webhook"
        requests.get(
            f"https://api.telegram.org/bot{self.cfg.token}/setWebhook",
            params={
                "url": webhook_url,
                "secret_token": self.cfg.secret_token,
                "allowed_updates": ["message", "chat_member", "callback_query"],
                "drop_pending_updates": True,
            },
            timeout=10,
        )
        logging.info(f"✅ Webhook configurado en: {webhook_url}")

        # ════════════════════════════════════════════════════════════
        # 🔥 PASO 3: CREAR SERVIDOR WEB CON TODAS LAS RUTAS
        # ════════════════════════════════════════════════════════════
        web_app = web.Application()
        web_app["bot"] = app
        
        template_path = os.path.join(os.path.dirname(__file__), "templates")
        aiohttp_jinja2.setup(web_app, loader=jinja2.FileSystemLoader(template_path))

        # Añadir TODAS las rutas ANTES de iniciar el servidor
        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/twitch/setup/callback", self.oauth.twitch_callback_setup)
        web_app.router.add_get("/twitch/callback", self.oauth.twitch_callback_user)
        web_app.router.add_get("/dashboard/{slug}", self.dashboard.page, name="dashboard_page")
        web_app.router.add_post("/dashboard/auditnow/{slug}", self.dashboard.trigger_audit)
        web_app.router.add_get("/health", lambda _: web.Response(text="OK"))
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo correctamente."))

        # ════════════════════════════════════════════════════════════
        # 🔥 PASO 4: INICIAR SERVIDOR
        # ════════════════════════════════════════════════════════════
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logging.info(f"🌐 Servidor web iniciado en puerto {port}")

        await app.initialize()
        await app.start()
        logging.info("💜 Bot de Telegram iniciado correctamente")
        logging.info(f"📡 Escuchando webhooks en: {webhook_url}")
        
        await asyncio.Event().wait()
