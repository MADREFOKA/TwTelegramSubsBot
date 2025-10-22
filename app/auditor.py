import logging
import datetime
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database


# ──────────────────────────────────────────────
# 🔍 Ejecuta una auditoría para un streamer
# ──────────────────────────────────────────────
async def run_audit_for_slug(db: Database, slug: str):
    """
    Ejecuta una auditoría básica: cuenta miembros en el grupo vinculado
    del streamer. Los campos 'expulsados', 'sin_vinculo' y 'fallos'
    se mantienen en 0 para compatibilidad futura.
    """
    try:
        # Buscar miembros asociados al grupo de este streamer
        logs = await db.all("""
            SELECT telegram_id
            FROM group_members
            WHERE group_id = (
                SELECT group_id FROM broadcasters WHERE slug=%s
            )
        """, slug)

        total = len(logs)
        expulsados = 0
        sin_vinculo = 0
        fallos = 0

        await db.exec("""
            INSERT INTO audit_logs (
                broadcaster_id, group_id, etiqueta_semana,
                total_miembros, expulsados, sin_vinculo, fallos, streamer_slug
            )
            VALUES (
                (SELECT broadcaster_id FROM broadcasters WHERE slug=%s),
                (SELECT group_id FROM broadcasters WHERE slug=%s),
                %s, %s, %s, %s, %s, %s
            )
        """, slug, slug,
            datetime.date.today().strftime("Semana %W"),
            total, expulsados, sin_vinculo, fallos, slug
        )

        logging.info(f"✅ Auditoría registrada para {slug} ({total} miembros).")

    except Exception as e:
        logging.error(f"❌ Error en run_audit_for_slug({slug}): {e}")
        raise


# ──────────────────────────────────────────────
# 🧹 Clase principal de auditorías
# ──────────────────────────────────────────────
class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    # ─────────────────────────────
    # /auditnow — Manual desde Telegram
    # ─────────────────────────────
    async def cmd_auditnow(self, update, context):
        """Auditoría manual: ejecuta en silencio y muestra solo el botón al Dashboard."""
        user = update.effective_user
        chat_type = update.effective_chat.type

        # Evitar uso en grupos
        if chat_type != "private":
            return await update.message.reply_text("⚠️ Este comando solo puede usarse en chat privado.")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        try:
            await run_audit_for_slug(self.db, b["slug"])

            slug = b["slug"]
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Abrir Dashboard", url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}")]
            ])

            await update.message.reply_text(
                "✅ Auditoría finalizada. Revisa tu Dashboard.",
                reply_markup=keyboard
            )

        except Exception as e:
            logging.error(f"Error en auditoría manual ({b['slug']}): {e}")
            await update.message.reply_text("⚠️ Error al ejecutar la auditoría. Inténtalo más tarde.")

    # ─────────────────────────────
    # on_member_update (placeholder)
    # ─────────────────────────────
    async def on_member_update(self, update, context):
        """Futuro: controlará cuando los miembros se unen o salen de los grupos."""
        pass

    # ─────────────────────────────
    # Auditoría semanal automática
    # ─────────────────────────────
    def schedule_weekly_audit(self, job_queue):
        """Programa una auditoría automática semanal."""
        from telegram.ext import CallbackContext

        async def _weekly_task(context: 'CallbackContext'):
            rows = await self.db.all("SELECT slug FROM broadcasters")
            for r in rows:
                try:
                    await run_audit_for_slug(self.db, r["slug"])
                except Exception as e:
                    logging.error(f"Error auditoría semanal {r['slug']}: {e}")

        # 604800 segundos = 1 semana
        try:
            job_queue.run_repeating(_weekly_task, interval=604800, first=30)
            logging.info("🗓️ Auditoría semanal programada correctamente.")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo programar la auditoría semanal: {e}")
