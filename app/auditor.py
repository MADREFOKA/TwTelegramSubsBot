# app/auditor.py
import logging
import datetime
import time
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database

# ──────────────────────────────────────────────
# 🔍 AUDITORÍA COMPLETA (elimina no subs, no vinculados e inactivos)
# ──────────────────────────────────────────────
async def run_full_audit(bot, db: Database, slug: str):
    try:
        # Obtener streamer
        b = await db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or not b.get("group_id"):
            logging.warning(f"⏭️ Saltando auditoría: {slug} sin grupo vinculado.")
            return

        group_id = b["group_id"]
        broadcaster_id = b["broadcaster_id"]
        logging.info(f"🔎 Iniciando auditoría completa para {slug} ({group_id})")

        # Obtener miembros y privilegiados
        members = await db.all("""
            SELECT telegram_id, username, is_subscribed, twitch_login, joined_at
            FROM group_members
            WHERE group_id=%s
        """, group_id)
        privileged = set(await db.get_privileged_ids(slug))

        total = len(members)
        expulsados = 0
        sin_vinculo = 0
        fallos = 0
        ahora = datetime.datetime.utcnow()

        for m in members:
            tg_id = m["telegram_id"]
            if tg_id in privileged or tg_id == b["owner_telegram_id"]:
                continue  # No expulsar al streamer ni a privilegiados

            motivo = None

            # 🔹 Caso 1: no vinculado
            if not m.get("twitch_login"):
                motivo = "no vinculado"
                sin_vinculo += 1

            # 🔹 Caso 2: inactivo (más de 30 días sin actividad)
            elif m.get("joined_at") and (ahora - m["joined_at"]).days > 30:
                motivo = "inactivo"

            # 🔹 Caso 3: no sub
            elif not m.get("is_subscribed"):
                motivo = "no sub"

            # Expulsar si aplica
            if motivo:
                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    await bot.unban_chat_member(group_id, tg_id)  # Para que pueda volver si se renueva sub
                    expulsados += 1
                    logging.info(f"🚫 Expulsado @{m.get('username')} ({tg_id}) — {motivo}")
                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar {tg_id}: {e}")

        # Registrar auditoría
        await db.exec("""
            INSERT INTO audit_logs (
                broadcaster_id, group_id, etiqueta_semana,
                total_miembros, expulsados, sin_vinculo, fallos, streamer_slug
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
            broadcaster_id, group_id, datetime.date.today().strftime("Semana %W"),
            total, expulsados, sin_vinculo, fallos, slug
        )
        # ─────────────────────────────
        # 🧹 LIMPIEZA DE oauth_states (registros viejos)
        # ─────────────────────────────
        try:
            cutoff = int(time.time()) - 86400  # 24h = 86400 segundos
            deleted = await db.exec(
                "DELETE FROM oauth_states WHERE created_at < %s",
                cutoff
            )
            logging.info("🧹 oauth_states limpiado (entradas >24h eliminadas).")
        except Exception as e:
            logging.warning(f"⚠️ Error limpiando oauth_states: {e}")
        logging.info(f"✅ Auditoría completada para {slug}: {expulsados} expulsados.")

    except Exception as e:
        logging.exception(f"❌ Error en run_full_audit({slug}): {e}")
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
        """Ejecuta una auditoría completa del grupo."""
        user = update.effective_user
        chat_type = update.effective_chat.type

        if chat_type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        slug = b["slug"]
        group_id = b.get("group_id")

        if not group_id:
            return await update.message.reply_text("⚠️ No tienes ningún grupo premium vinculado.")

        try:
            await run_full_audit(context.bot, self.db, slug)

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Abrir Dashboard",
                url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}")
            ]])

            await update.message.reply_text("✅ Auditoría completada. Revisa tu Dashboard.", reply_markup=keyboard)

        except Exception as e:
            logging.error(f"Error en auditoría manual ({slug}): {e}")
            await update.message.reply_text("⚠️ Error al ejecutar la auditoría.")

    # ─────────────────────────────
    # Auditoría automática semanal (cada lunes)
    # ─────────────────────────────
    def schedule_weekly_audit(self, job_queue):
        from telegram.ext import CallbackContext

        async def _weekly_task(context: 'CallbackContext'):
            rows = await self.db.all("SELECT slug FROM broadcasters")
            for r in rows:
                try:
                    await run_full_audit(context.bot, self.db, r["slug"])
                except Exception as e:
                    logging.error(f"Error auditoría semanal {r['slug']}: {e}")

        # Ejecutar todos los lunes a las 9:00 UTC
        job_queue.run_repeating(_weekly_task, interval=604800, first=60)
        logging.info("🗓️ Auditoría automática semanal programada (lunes).")
