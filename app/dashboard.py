import logging
import aiohttp_jinja2
from aiohttp import web
from app.auditor import run_audit_for_slug

class Dashboard:
    def __init__(self, db):
        self.db = db

    @aiohttp_jinja2.template("dashboard.html")
    async def page(self, request):
        slug = request.match_info.get("slug").lower()
        token = request.query.get("token")

        # Buscar streamer y avatar
        streamer = await self.db.one("""
            SELECT display_name, slug, COALESCE(profile_image_url, '') AS profile_image_url
            FROM broadcasters
            WHERE LOWER(slug)=%s AND dashboard_token=%s
        """, slug, token)

        if not streamer:
            raise web.HTTPFound("/")

        # Cargar logs recientes (garantiza coincidencia por slug normalizado)
        logs = await self.db.all("""
            SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
            FROM audit_logs
            WHERE LOWER(streamer_slug)=%s
            ORDER BY fecha_auditoria DESC
            LIMIT 20
        """, slug)

        resumen = logs[0] if logs else None
        avatar_url = streamer["profile_image_url"] or "https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"

        return {"streamer": streamer, "logs": logs, "resumen": resumen, "token": token, "avatar_url": avatar_url}

    async def trigger_audit(self, request):
        try:
            slug = request.match_info.get("slug").lower()
            token = request.query.get("token")

            b = await self.db.one(
                "SELECT * FROM broadcasters WHERE LOWER(slug)=%s AND dashboard_token=%s",
                slug, token
            )
            if not b:
                return web.json_response({"status": "error", "msg": "Token inválido o streamer no encontrado."}, status=403)

            await run_audit_for_slug(self.db, b["slug"])
            return web.json_response({"status": "ok", "msg": "✅ Auditoría completada correctamente."})
        except Exception as e:
            logging.exception("Error en trigger_audit:")
            return web.json_response({"status": "error", "msg": str(e)}, status=500)
