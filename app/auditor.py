# app/auditor.py
import logging
import datetime
import time
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database
from datetime import time as dtime

# ──────────────────────────────────────────────
# 🔍 AUDITORÍA COMPLETA (elimina no subs, no vinculados e inactivos)
# ──────────────────────────────────────────────
async def run_full_audit(bot, db, slug: str):
    """
    Ejecuta una auditoría completa:
    - Expulsa usuarios no vinculados, no suscriptores o inactivos.
    - Expulsa también cualquier usuario detectado en el grupo
      que no esté en group_members ni en la lista de privilegiados.
    """
    try:
        # Obtener streamer y grupo
        b = await db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or not b.get("group_id"):
            logging.warning(f"⏭️ Saltando auditoría: {slug} sin grupo vinculado.")
            return

        group_id = b["group_id"]
        broadcaster_id = b["broadcaster_id"]
        logging.info(f"🔎 Iniciando auditoría completa para {slug} ({group_id})")

        # Obtener miembros registrados en BD y privilegiados
        members = await db.all("""
            SELECT telegram_id, username, is_subscribed, twitch_login, joined_at
            FROM group_members
            WHERE group_id=%s
        """, group_id)
        privileged = set(await db.get_privileged_ids(slug))

        # Crear sets para búsquedas rápidas
        db_member_ids = {m["telegram_id"] for m in members}
        privileged_ids = privileged | {b["owner_telegram_id"]}

        total = len(members)
        expulsados = 0
        sin_vinculo = 0
        fallos = 0
        ahora = datetime.datetime.utcnow()

        # 🔹 Paso 1 — Expulsar miembros del group_members no válidos
        for m in members:
            tg_id = m["telegram_id"]
            if tg_id in privileged_ids:
                continue

            motivo = None
            if not m.get("twitch_login"):
                motivo = "no vinculado"
                sin_vinculo += 1
            elif not m.get("is_subscribed"):
                motivo = "no sub"
            elif m.get("joined_at") and (ahora - m["joined_at"]).days > 30:
                motivo = "inactivo"

            if motivo:
                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    await bot.unban_chat_member(group_id, tg_id)
                    expulsados += 1
                    logging.info(f"🚫 Expulsado @{m.get('username')} ({tg_id}) — {motivo}")
                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar {tg_id}: {e}")

        # 🔹 Paso 2 — Revisar mensajes recientes y detectar usuarios fuera de group_members
        try:
            async for message in bot.get_chat_history(chat_id=group_id, limit=2000):
                user = message.from_user
                if not user:
                    continue
                if user.id in db_member_ids or user.id in privileged_ids:
                    continue  # Ya registrado o privilegiado

                # Usuario no registrado => expulsar
                try:
                    await bot.ban_chat_member(group_id, user.id)
                    await bot.unban_chat_member(group_id, user.id)
                    expulsados += 1
                    logging.info(f"🚫 Expulsado no registrado: @{user.username} ({user.id})")
                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar no registrado {user.id}: {e}")
        except Exception as e:
            logging.warning(f"⚠️ No se pudieron analizar mensajes recientes: {e}")

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
        """
        Programa la auditoría automática para ejecutarse
        todos los lunes a las 09:00 UTC.
        """
        from telegram.ext import CallbackContext
    
        async def _weekly_task(context: 'CallbackContext'):
            rows = await self.db.all("SELECT slug FROM broadcasters")
            for r in rows:
                try:
                    await run_full_audit(context.bot, self.db, r["slug"])
                except Exception as e:
                    logging.error(f"Error auditoría semanal {r['slug']}: {e}")
    
        try:
            job_queue.run_daily(
                _weekly_task,
                time=dtime(hour=9, minute=0),  # 09:00 UTC
                days=(0,),  # 0 = Lunes
                name="weekly_full_audit"
            )
            logging.info("🗓️ Auditoría semanal programada: cada lunes a las 09:00 UTC")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo programar la auditoría semanal: {e}")
            
