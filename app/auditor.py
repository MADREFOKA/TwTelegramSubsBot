import logging
import datetime
from telegram.ext import JobQueue

class Auditor:
    def __init__(self, db):
        self.db = db

    def schedule_weekly_audit(self, job_queue: JobQueue):
        job_queue.run_daily(self.run_all_audits, time=datetime.time(hour=9, minute=0))

    async def run_all_audits(self, context):
        broadcasters = await self.db.all("SELECT slug, group_id FROM broadcasters")
        for b in broadcasters:
            try:
                await self.run_audit_for_slug(b["slug"], b["group_id"])
            except Exception as e:
                logging.error(f"❌ Error en auditoría {b['slug']}: {e}")

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
