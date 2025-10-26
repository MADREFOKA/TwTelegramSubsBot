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
            audit_done = request.query.get("audit_done")

            # Validar acceso al Dashboard
            streamer = await self.db.one("""
                SELECT display_name, slug, profile_image_url, dashboard_token
                FROM broadcasters
                WHERE slug=%s AND dashboard_token=%s
            """, slug, token)

            if not streamer:
                raise web.HTTPFound("/")

            # Obtener logs de auditoría (últimas 20)
            logs = await self.db.all("""
                SELECT total_miembros, expulsados, fallos, fecha_auditoria
                FROM audit_logs
                WHERE streamer_slug=%s
                ORDER BY fecha_auditoria DESC
                LIMIT 20
            """, slug)

            # 🔧 Formatear fechas legibles
            for l in logs:
                if l.get("fecha_auditoria"):
                    l["fecha_auditoria"] = l["fecha_auditoria"].strftime("%d/%m/%Y %H:%M")

            resumen = logs[0] if logs else None

            avatar_url = (
                streamer.get("profile_image_url")
                or "https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"
            )

            # Obtener lista de privilegiados
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
                "audit_done": audit_done,
                "base_url": os.getenv("BASE_URL"),
            }

        except Exception as e:
            logging.exception("Error mostrando dashboard:")
            raise web.HTTPInternalServerError(text=str(e))

    # ───────────────────────────────
    # 🔁 Forzar auditoría desde el Dashboard
    # ───────────────────────────────
    async def trigger_audit(self, request: web.Request):
        try:
            slug = request.match_info.get("slug")
            token = request.query.get("token")

            # Validar acceso del streamer
            b = await self.db.one(
                "SELECT * FROM broadcasters WHERE slug=%s AND dashboard_token=%s",
                slug, token
            )
            if not b:
                return web.json_response(
                    {"status": "error", "msg": "Token inválido o no autorizado."},
                    status=403
                )

            # Ejecutar auditoría
            app = request.app
            bot_instance = app.get("bot")
            if not bot_instance:
                raise web.HTTPInternalServerError(text="❌ No se encontró instancia del bot en la app.")

            await run_full_audit(bot_instance.app.bot, self.db, b["slug"], bot_instance.refresh_twitch_token)

            # Redirigir al dashboard con notificación
            raise web.HTTPFound(f"/dashboard/{slug}?token={token}&audit_done=1")

        except Exception as e:
            logging.exception("Error en trigger_audit:")
            return web.json_response(
                {"status": "error", "msg": f"Error interno: {e}"},
                status=500
            )
            
    # ───────────────────────────────
    # 🔍 Detalles de una auditoría (solo expulsados o fallos)
    # ───────────────────────────────
    @aiohttp_jinja2.template("audit_details.html")
    async def audit_details(self, request):
        try:
            audit_id = request.match_info.get("audit_id")
            if not audit_id:
                raise web.HTTPBadRequest(text="Falta audit_id.")
    
            # Buscar la auditoría
            audit = await self.db.one("""
                SELECT * FROM audit_logs WHERE id=%s
            """, audit_id)
            if not audit:
                raise web.HTTPNotFound(text="Auditoría no encontrada.")
    
            # Buscar detalles de usuarios expulsados o con fallos
            detalles = await self.db.all("""
                SELECT username, motivo, resultado
                FROM audit_details
                WHERE audit_id=%s
                AND (resultado='expulsado' OR resultado='fallo')
                ORDER BY resultado DESC
            """, audit_id)
    
            return {
                "audit": audit,
                "detalles": detalles
            }
    
        except Exception as e:
            logging.exception("Error mostrando detalles de auditoría:")
            raise web.HTTPInternalServerError(text=str(e))
