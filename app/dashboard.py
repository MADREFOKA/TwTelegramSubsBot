# app/dashboard.py
import logging
from aiohttp import web
import aiohttp_jinja2

class Dashboard:
    def __init__(self, db):
        self.db = db

    # ───────────────────────────────
    # 📊 Página principal del Dashboard
    # ───────────────────────────────
    @aiohttp_jinja2.template("dashboard.html")
    async def page(self, request: web.Request):
        try:
            slug = request.match_info["slug"]
            token = request.query.get("token")

            # Validar acceso al Dashboard
            streamer = await self.db.one("""
                SELECT display_name, slug, profile_image_url, broadcaster_id, group_id
                FROM broadcasters
                WHERE slug=%s AND dashboard_token=%s
            """, slug, token)

            if not streamer:
                raise web.HTTPFound("/")

            # Obtener últimas auditorías
            auditorias = await self.db.all("""
                SELECT id, total_miembros, expulsados, fallos, fecha_auditoria
                FROM audit_logs
                WHERE streamer_slug=%s
                ORDER BY fecha_auditoria DESC
                LIMIT 15
            """, slug)

            avatar_url = (
                streamer.get("profile_image_url")
                or "https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"
            )

            return {
                "streamer": streamer,
                "auditorias": auditorias,
                "token": token,
                "avatar_url": avatar_url,
            }

        except Exception as e:
            logging.exception("Error mostrando dashboard:")
            raise web.HTTPInternalServerError(text=str(e))

    # ───────────────────────────────
    # 🔍 Detalles de auditoría
    # ───────────────────────────────
    async def audit_details(self, request: web.Request):
        try:
            audit_id = request.match_info.get("audit_id")

            detalles = await self.db.all("""
                SELECT username, twitch_login, accion, motivo, timestamp
                FROM audit_details
                WHERE audit_id=%s AND accion IN ('expulsado', 'fallo')
                ORDER BY id ASC
            """, audit_id)

            for d in detalles:
                if not d.get("username"):
                    d["username"] = "(usuario desconocido)"
                if not d.get("twitch_login"):
                    d["twitch_login"] = "—"

            return web.json_response(detalles)

        except Exception as e:
            logging.exception("Error cargando detalles de auditoría:")
            return web.json_response({"error": str(e)}, status=500)
