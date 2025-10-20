import logging
from aiohttp import web
import json

class Dashboard:
    def __init__(self, db):
        self.db = db

    async def page(self, request):
        slug = request.match_info.get("slug")
        token = request.query.get("token")

        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or b.get("dashboard_token") != token:
            return web.Response(text="❌ Acceso no autorizado.", status=403)

        logs = await self.db.all("""
            SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
            FROM audit_logs
            WHERE streamer_slug=%s
            ORDER BY fecha_auditoria DESC
            LIMIT 20
        """, slug)

        labels = [l["etiqueta_semana"] or "—" for l in logs]
        members = [l["total_miembros"] or 0 for l in logs]
        expelled = [l["expulsados"] or 0 for l in logs]
        nolink = [l["sin_vinculo"] or 0 for l in logs]
        fails = [l["fallos"] or 0 for l in logs]

        context = {
            "slug": slug,
            "display_name": b["display_name"],
            "logs": logs,
            "chart_labels": json.dumps(labels[::-1]),
            "chart_members": json.dumps(members[::-1]),
            "chart_expelled": json.dumps(expelled[::-1]),
            "chart_nolink": json.dumps(nolink[::-1]),
            "chart_fails": json.dumps(fails[::-1]),
        }

        return web.Response(
            text=open("app/templates/dashboard.html", encoding="utf-8").read()
                .replace("{{data}}", json.dumps(context)),
            content_type="text/html"
        )
