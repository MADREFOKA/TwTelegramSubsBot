import aiohttp_jinja2
from aiohttp import web
import asyncio
import logging
from app.auditor import run_full_audit


class Dashboard:
    def __init__(self, db, cfg=None, refresh_twitch_token=None):
        self.db = db
        self.cfg = cfg
        self.refresh_twitch_token = refresh_twitch_token

    # ───────────────────────────────
    # 📊 PÁGINA PRINCIPAL DEL DASHBOARD
    # ───────────────────────────────
    @aiohttp_jinja2.template("dashboard.html")
    async def page(self, request):
        slug = request.match_info.get("slug")
        token = request.rel_url.query.get("token")

        if not slug or not token:
            return web.Response(text="❌ Falta token o slug.", status=400)

        b = await self.db.one(
            "SELECT * FROM broadcasters WHERE slug=%s AND dashboard_token=%s",
            slug, token
        )
        if not b:
            return web.Response(text="❌ Token inválido o streamer no encontrado.", status=403)

        # Obtener últimas auditorías
        audits = await self.db.all("""
            SELECT id, total_miembros, expulsados, fallos, fecha_auditoria
            FROM audit_logs
            WHERE streamer_slug=%s
            ORDER BY fecha_auditoria DESC
        """, slug)

        last_audit = audits[0] if audits else None

        # Datos para la gráfica
        chart_labels = [a["fecha_auditoria"].strftime("%d/%m") for a in reversed(audits[-10:])]
        chart_values = [a["expulsados"] for a in reversed(audits[-10:])]

        return {
            "streamer": b["display_name"],
            "slug": slug,
            "avatar": b.get("profile_image_url"),
            "dashboard_token": token,
            "audits": audits,
            "last_audit": last_audit,
            "chart_labels": chart_labels,
            "chart_values": chart_values,
        }

    # ───────────────────────────────
    # 🚀 FORZAR AUDITORÍA MANUAL
    # ───────────────────────────────
    async def trigger_audit(self, request):
        slug = request.match_info.get("slug")
        token = request.rel_url.query.get("token")

        if not slug or not token:
            return web.Response(text="❌ Falta token o slug.", status=400)

        b = await self.db.one(
            "SELECT * FROM broadcasters WHERE slug=%s AND dashboard_token=%s",
            slug, token
        )
        if not b:
            return web.Response(text="❌ Token inválido o streamer no encontrado.", status=403)

        tg_app = request.app.get("bot")
        if not tg_app or not self.refresh_twitch_token:
            return web.Response(text="❌ Bot no inicializado.", status=500)

        # En segundo plano: una auditoría grande tarda minutos y la petición HTTP caducaría
        asyncio.create_task(
            run_full_audit(tg_app.bot, self.db, self.cfg, slug, self.refresh_twitch_token)
        )
        logging.info(f"🚀 Auditoría forzada desde dashboard para {slug}.")
        return web.Response(text="⏳ Auditoría iniciada. Actualiza la página en unos minutos.")
