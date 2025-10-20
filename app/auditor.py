import logging
import datetime

class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    def schedule_weekly_audit(self, job_queue):
        """Programa la auditoría semanal (lunes 9:00)."""
        try:
            job_queue.run_daily(self.run_all_audits, time=datetime.time(hour=9, minute=0, second=0))
            logging.info("🗓️ Auditoría semanal programada correctamente.")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo programar auditoría semanal: {e}")

    async def run_all_audits(self, context):
        broadcasters = await self.db.all("SELECT slug, group_id FROM broadcasters WHERE group_id IS NOT NULL")
        for b in broadcasters:
            try:
                await self.run_audit_for_slug(b["slug"], b["group_id"])
            except Exception as e:
                logging.error(f"❌ Error en auditoría de {b['slug']}: {e}")

    async def run_audit_for_slug(self, slug, group_id):
        now = datetime.datetime.now()
        etiqueta = f"Semana {now.strftime('%d/%m/%Y')}"
        total_miembros = 0
        expulsados = 0
        sin_vinculo = 0
        fallos = 0

        await self.db.exec("""
            INSERT INTO audit_logs (streamer_slug, etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, slug, etiqueta, total_miembros, expulsados, sin_vinculo, fallos, now)
