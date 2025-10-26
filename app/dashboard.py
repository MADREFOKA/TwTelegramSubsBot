# app/dashboard.py
import os
import logging
import aiohttp_jinja2
from aiohttp import web
from app.auditor import run_full_audit

class Dashboard:
    def __init__(self, db):
        self.db = db

    # ───────────────────────────────
    # 📊 Página principal del Dashboard
    # ───────────────────────────────
    @aiohttp_jinja2.template("dashboard.html")
    async def page(self, request):
        try:
            slug = request.match_info.get("slug")
            token = request.query.get("token")

            # Validar acceso al Dashboard
            streamer = await self.db.one("""
                SELECT display_name, slug, profile_image_url
                FROM broadcasters
                WHERE slug=%s AND dashboard_token=%s
            """, slug, token)

            if not streamer:
                raise web.HTTPFound("/")

            # Obtener logs de auditoría
            logs = await self.db.all("""
                SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
                FROM audit_logs
                WHERE streamer_slug=%s
                ORDER BY fecha_auditoria DESC
                LIMIT 20
            """, slug)

            # Convertir fechas legibles
            for l in logs:
                if l.get("fecha_auditoria"):
                    l["fecha_auditoria"] = l["fecha_auditoria"].strftime("%d/%m/%Y %H:%M")

            resumen = logs[0] if logs else None
            avatar_url = streamer.get("profile_image_url") or "https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"

            privileged = await self.db.all("""
                SELECT username, telegram_id, added_at
                FROM privileged_users
                WHERE streamer_slug=%s
                ORDER BY added_at DESC
            """, slug)

            return {
                "streamer": streamer,
                "logs": logs,
                "resumen": resumen,
                "avatar_url": avatar_url,
                "token": token,
                "privileged_users": privileged,
                "base_url": os.getenv("BASE_URL"),
            }

        except Exception as e:
            logging.exception("Error mostrando dashboard:")
            raise web.HTTPInternalServerError(text=str(e))

    # ───────────────────────────────
    # 🔁 Forzar auditoría desde Dashboard
    # ───────────────────────────────
    async def trigger_audit(self, request: web.Request):
        try:
            slug = request.match_info.get("slug")
            token = request.query.get("token")

            # Validar token del streamer
            b = await self.db.one(
                "SELECT * FROM broadcasters WHERE slug=%s AND dashboard_token=%s",
                slug, token
            )
            if not b:
                return web.json_response(
                    {"status": "error", "msg": "Token inválido o no autorizado."},
                    status=403
                )

            bot = request.app["bot"]
            refresh_twitch_token = request.app["refresh_twitch_token"]

            await run_full_audit(bot, self.db, slug, refresh_twitch_token)

            return web.json_response({
                "status": "ok",
                "msg": "✅ Auditoría completada correctamente."
            })

        except Exception as e:
            logging.exception("Error en trigger_audit:")
            return web.json_response(
                {"status": "error", "msg": f"Error interno: {e}"},
                status=500
            )
