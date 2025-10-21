import logging
import asyncio
import time
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from datetime import datetime, timedelta


class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    # ───────────────────────────────
    # 🔹 Comando manual: /auditnow
    # ───────────────────────────────
    async def cmd_auditnow(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Forza una auditoría inmediata desde Telegram."""
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero para vincular tu canal.")

        slug = b["slug"]
        logging.info(f"🧹 Iniciando auditoría manual para {slug}")

        # Ejecutar auditoría en segundo plano
        asyncio.create_task(run_audit_for_slug(self.db, slug))

        # ✅ Mensaje final con botón al Dashboard
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Abrir Dashboard", url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}")]
        ])
        await update.message.reply_text(
            "✅ Auditoría iniciada correctamente.\nRevisa los resultados en tu Dashboard.",
            reply_markup=keyboard
        )

    # ───────────────────────────────
    # 🔹 Auditoría semanal automática
    # ───────────────────────────────
    def schedule_weekly_audit(self, job_queue):
        """Programa auditorías automáticas semanales (una por streamer)."""
        async def job(context):
            try:
                broadcasters = await self.db.all("SELECT slug FROM broadcasters WHERE group_id IS NOT NULL")
                for b in broadcasters:
                    await run_audit_for_slug(self.db, b["slug"])
                logging.info("✅ Auditoría semanal automática completada.")
            except Exception as e:
                logging.error(f"❌ Error en auditoría semanal: {e}")

        # Ejecuta cada lunes a las 04:00 UTC
        job_queue.run_repeating(lambda ctx: asyncio.create_task(job(ctx)),
                                interval=7 * 24 * 3600,
                                first=5)

# ───────────────────────────────
# 🔹 Lógica de auditoría
# ───────────────────────────────
async def run_audit_for_slug(db, slug: str):
    """
    Simula el proceso de auditoría:
    - Calcula miembros, expulsados, sin vínculo y fallos aleatorios (ejemplo)
    - Registra el resultado en audit_logs
    """
    import random
    total = random.randint(50, 500)
    expulsados = random.randint(0, total // 10)
    sin_vinculo = random.randint(0, 5)
    fallos = random.randint(0, 3)

    semana = datetime.utcnow().strftime("Semana %W %Y")
    fecha = datetime.utcnow()

    try:
        await db.exec(
            """
            INSERT INTO audit_logs (broadcaster_id, group_id, etiqueta_semana,
                                    total_miembros, expulsados, sin_vinculo,
                                    fallos, fecha_auditoria, streamer_slug)
            VALUES (
                (SELECT broadcaster_id FROM broadcasters WHERE slug=%s),
                (SELECT group_id FROM broadcasters WHERE slug=%s),
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            slug, slug, semana, total, expulsados, sin_vinculo, fallos, fecha, slug
        )
        logging.info(f"🧾 Auditoría registrada correctamente para {slug}")
    except Exception as e:
        logging.exception(f"❌ Error registrando auditoría para {slug}: {e}")
        
