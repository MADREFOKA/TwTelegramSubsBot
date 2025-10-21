import logging
import datetime
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database


async def run_audit_for_slug(db: Database, slug: str):
    logs = await db.all("SELECT * FROM group_members WHERE streamer_slug=%s", slug)
    total = len(logs)
    expulsados = sum(1 for x in logs if x.get("expulsado"))
    sin_vinculo = sum(1 for x in logs if not x.get("vinculo"))
    fallos = sum(1 for x in logs if x.get("fallo"))

    await db.exec("""
        INSERT INTO audit_logs (broadcaster_id, group_id, etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, streamer_slug)
        VALUES (
            (SELECT broadcaster_id FROM broadcasters WHERE slug=%s),
            (SELECT group_id FROM broadcasters WHERE slug=%s),
            %s, %s, %s, %s, %s, %s
        )
    """, slug, slug, datetime.date.today().strftime("Semana %W"), total, expulsados, sin_vinculo, fallos, slug)


class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    async def cmd_auditnow(self, update, context):
        """Auditoría manual (silenciosa, solo aviso final con link al dashboard)."""
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        await run_audit_for_slug(self.db, b["slug"])

        slug = b["slug"]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Abrir Dashboard", url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}")]
        ])
        await update.message.reply_text("✅ Auditoría finalizada. Revisa tu Dashboard.", reply_markup=keyboard)

    async def on_member_update(self, update, context):
        pass

    def schedule_weekly_audit(self, job_queue):
        from telegram.ext import CallbackContext
        job_queue.run_repeating(self._weekly_task, interval=604800, first=10)

    async def _weekly_task(self, context: 'CallbackContext'):
        rows = await self.db.all("SELECT slug FROM broadcasters")
        for r in rows:
            try:
                await run_audit_for_slug(self.db, r["slug"])
            except Exception as e:
                logging.error(f"Error auditoría semanal {r['slug']}: {e}")
