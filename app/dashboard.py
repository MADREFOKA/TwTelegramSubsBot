import logging
from aiohttp import web
import aiohttp_jinja2


class Dashboard:
    def __init__(self, db):
        self.db = db

    async def page(self, request: web.Request):
        slug = request.match_info.get("slug")
        token = request.query.get("token")

        try:
            # Obtener streamer validando token privado
            streamer = await self.db.one(
                """
                SELECT display_name, slug, profile_image_url
                FROM broadcasters
                WHERE slug=%s AND dashboard_token=%s
                """,
                slug, token
            )

            if not streamer:
                return web.Response(text="❌ Acceso no autorizado.", status=403)

            # Recuperar hasta 20 auditorías más recientes
            audits = await self.db.all("""
                SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
                FROM audit_logs
                WHERE streamer_slug=%s
                ORDER BY fecha_auditoria DESC
                LIMIT 20
            """, slug)

            last_audit = audits[0] if audits else None

            # Renderizar HTML
            return aiohttp_jinja2.render_template(
                "dashboard.html",
                request,
                context={
                    "streamer": streamer,
                    "audits": audits or [],
                    "last_audit": last_audit,
                },
            )

        except Exception as e:
            logging.exception("Error mostrando dashboard:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)
