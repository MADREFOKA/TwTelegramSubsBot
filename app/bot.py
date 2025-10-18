# app/bot.py
import os
import time
import logging
import asyncio
import requests
import aiohttp
from datetime import datetime, timezone
from aiohttp import web
from telegram.ext import ApplicationBuilder, CommandHandler, ChatMemberHandler
from telegram import Update
from jinja2 import Environment, FileSystemLoader

from app.db import Database
from app.security import TokenCipher
from app.commands import Commands
from app.oauth import OAuthHandlers
from app.auditor import Auditor


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


class TwitchSubBot:
    def __init__(self, db: Database):
        self.db = db
        self.cfg = Config()
        self.cipher = TokenCipher()
        self.app = None

        # Componentes principales
        self.cmds = Commands(self.db, self.cfg, self.cipher)
        self.oauth = OAuthHandlers(
            self.cfg, self.db, self.send_tg,
            self.refresh_twitch_token, self.days_until, self.cipher
        )
        self.auditor = Auditor(self.db, self.cfg, self.send_tg, self.refresh_twitch_token)

    # ----------------------------------------------------------------------
    # UTILIDAD PARA ENVIAR MENSAJES A TELEGRAM
    # ----------------------------------------------------------------------
    async def send_tg(self, chat_id, text, md=False, html=False):
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if md:
            payload["parse_mode"] = "Markdown"
        elif html:
            payload["parse_mode"] = "HTML"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    # ----------------------------------------------------------------------
    # REFRESCO DE TOKENS DE TWITCH (con cifrado Fernet)
    # ----------------------------------------------------------------------
    async def refresh_twitch_token(self, broadcaster_row: dict) -> str:
        """Devuelve un access_token válido, refrescando si es necesario."""
        # 🔓 Descifrar tokens guardados
        access_token = self.cipher.decrypt(broadcaster_row["access_token"])
        refresh_token = self.cipher.decrypt(broadcaster_row.get("refresh_token"))
        obtained = broadcaster_row["token_obtained_at"]
        expires = broadcaster_row["token_expires_in"]

        if not refresh_token:
            return access_token

        # Si faltan más de 24h, seguimos usando el actual
        if time.time() - obtained < max(0, expires - 86400):
            return access_token

        try:
            data = requests.post(self.cfg.TW_TOKEN, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret
            }, timeout=15).json()
            if "access_token" in data:
                access_token = data["access_token"]
                refresh_token = data.get("refresh_token", refresh_token)

                # 🔒 Cifrar los nuevos tokens antes de guardarlos
                enc_access = self.cipher.encrypt(access_token)
                enc_refresh = self.cipher.encrypt(refresh_token)

                await self.db.exec("""
                    UPDATE broadcasters
                    SET access_token=%s, refresh_token=%s, token_obtained_at=%s, token_expires_in=%s
                    WHERE broadcaster_id=%s
                """, enc_access, enc_refresh, int(time.time()), data.get("expires_in", expires),
                   broadcaster_row["broadcaster_id"])
        except Exception as e:
            logging.warning(f"Error al refrescar token de Twitch: {e}")
        return access_token

    # ----------------------------------------------------------------------
    # UTILIDAD: días restantes de suscripción
    # ----------------------------------------------------------------------
    def days_until(self, iso_ts: str):
        try:
            renew_time = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            diff = renew_time - now
            days = max(diff.days, 0)
            return renew_time.strftime("%d/%m/%Y %H:%M UTC"), days
        except Exception:
            return "desconocida", 0

    # ----------------------------------------------------------------------
    # WEBHOOKS DE TELEGRAM
    # ----------------------------------------------------------------------
    async def webhook_handler(self, request: web.Request):
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if token and token != self.cfg.secret_token:
            return web.Response(text="Unauthorized", status=403)
        try:
            data = await request.json()
            update = Update.de_json(data, self.app.bot)
            await self.app.process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            logging.exception("Error procesando webhook:")
            return web.Response(text=f"Error interno: {e}", status=500)

    # ----------------------------------------------------------------------
    # INICIO DEL BOT Y SERVIDOR WEB
    # ----------------------------------------------------------------------
    async def start(self):
        await self.db.init()

        app = ApplicationBuilder().token(self.cfg.token).build()
        self.app = app

        # Comandos principales
        app.add_handler(CommandHandler("setup", self.cmds.cmd_setup))
        app.add_handler(CommandHandler("start", self.cmds.cmd_start))
        app.add_handler(CommandHandler("setgroup", self.cmds.cmd_setgroup))
        app.add_handler(CommandHandler("setfreegroup", self.cmds.cmd_setfreegroup))
        app.add_handler(CommandHandler("regendashlink", self.cmds.cmd_regendashlink))

        # Auditoría y privilegios
        app.add_handler(CommandHandler("auditnow", self.auditor.cmd_auditnow))
        app.add_handler(CommandHandler("priv_add", self.auditor.cmd_priv_add))
        app.add_handler(CommandHandler("priv_remove", self.auditor.cmd_priv_remove))
        app.add_handler(CommandHandler("priv_list", self.auditor.cmd_priv_list))
        app.add_handler(ChatMemberHandler(self.auditor.on_member_update,
                                          chat_member_types=["member", "administrator", "left", "kicked"]))

        self.auditor.schedule_weekly_audit(app.job_queue)

        # Configurar webhook
        webhook_url = f"{self.cfg.base_url}/webhook"
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.cfg.token}/setWebhook",
                params={
                    "url": webhook_url,
                    "secret_token": self.cfg.secret_token,
                    "allowed_updates": ["message", "chat_member"],
                    "drop_pending_updates": True,
                },
                timeout=10,
            )
            if r.status_code == 200:
                logging.info(f"✅ Webhook configurado correctamente: {webhook_url}")
            else:
                logging.warning(f"⚠️ Error al configurar webhook: {r.text}")
        except Exception as e:
            logging.error(f"❌ No se pudo establecer el webhook: {e}")

        # Servidor web aiohttp
        web_app = web.Application()
        web_app["bot"] = app

        templates = Environment(loader=FileSystemLoader("app/templates"))

        async def dashboard_page(request):
            slug = request.match_info.get("slug")
            token = request.query.get("token")

            if not slug or not token:
                return web.Response(text="❌ Acceso denegado (falta token).", status=403)

            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
            if not b:
                return web.Response(text="❌ Streamer no encontrado.", status=404)

            if b.get("dashboard_token") != token:
                return web.Response(text="🚫 Acceso denegado. Token inválido.", status=403)

            logs = await self.db.all("""
                SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
                FROM audit_logs
                WHERE streamer_slug=%s
                ORDER BY fecha_auditoria DESC
                LIMIT 20
            """, slug)

            template = templates.get_template("dashboard.html")
            html = template.render(slug=slug, logs=logs, total_audits=len(logs))
            return web.Response(text=html, content_type="text/html")

        # Rutas
        web_app.router.add_post("/webhook", self.webhook_handler)
        web_app.router.add_get("/twitch/setup/callback", self.oauth.twitch_callback_setup)
        web_app.router.add_get("/twitch/callback", self.oauth.twitch_callback_user)
        web_app.router.add_get("/dashboard/{slug}", dashboard_page)
        web_app.router.add_get("/ping", lambda _: web.Response(text="🏓 Pong!"))
        web_app.router.add_get("/", lambda _: web.Response(text="✅ Bot operativo correctamente."))

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()

        await app.initialize()
        await app.start()
        logging.info("💜 Bot y servidor web iniciados correctamente.")
        logging.info("💫 Esperando eventos de Telegram y Twitch...")

        await asyncio.sleep(1)
        await asyncio.Event().wait()
