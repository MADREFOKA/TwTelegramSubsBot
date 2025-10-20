import logging
from aiohttp import web

class Dashboard:
    def __init__(self, db):
        self.db = db

    async def page(self, request):
        slug = request.match_info["slug"]
        token = request.query.get("token")
        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s AND dashboard_token=%s", slug, token)
        if not b:
            return web.Response(text="❌ Acceso no autorizado.", status=403)

        logs = await self.db.all("""
            SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
            FROM audit_logs
            WHERE streamer_slug=%s
            ORDER BY fecha_auditoria DESC
            LIMIT 20
        """, slug)

        html = "<html><head><title>Dashboard</title></head><body><h2>📊 Estadísticas de Auditoría</h2><ul>"
        for l in logs:
            html += f"<li><b>{l['etiqueta_semana']}</b>: {l['expulsados']} expulsados / {l['total_miembros']} miembros</li>"
        html += "</ul></body></html>"
        return web.Response(text=html, content_type="text/html")
