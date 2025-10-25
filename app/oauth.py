# app/auditor.py
import logging
import datetime
import requests
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import time as dtime
from app.db import Database
from app.security import TokenCipher


# ──────────────────────────────────────────────
# 🔍 AUDITORÍA COMPLETA (verifica en Twitch por twitch_id)
# ──────────────────────────────────────────────
async def run_full_audit(bot, db: Database, slug: str):
    """
    Ejecuta una auditoría completa:
    - Verifica en Twitch si cada miembro sigue siendo sub.
    - Expulsa no subs, no vinculados o inactivos.
    - Elimina de la BD los expulsados y actualiza is_subscribed.
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

        # Obtener miembros registrados y privilegiados
        members = await db.all("""
            SELECT telegram_id, username, is_subscribed, twitch_id, joined_at
            FROM group_members
            WHERE group_id=%s
        """, group_id)
        privileged = set(await db.get_privileged_ids(slug))
        privileged_ids = privileged | {b["owner_telegram_id"]}

        total = len(members)
        expulsados = 0
        sin_vinculo = 0
        fallos = 0
        ahora = datetime.datetime.utcnow()

        # Preparar token de Twitch (descifrar)
        cipher = TokenCipher()
        try:
            access_token = cipher.decrypt(b["access_token"])
        except Exception as e:
            logging.warning(f"⚠️ Error descifrando token para {slug}: {e}")
            access_token = None

        headers_twitch = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": b.get("client_id") or "",
        }

        # ────────────────────────────────
        # 🔹 Paso 1 — Revisar cada miembro
        # ────────────────────────────────
        for m in members:
            tg_id = m["telegram_id"]
            if tg_id in privileged_ids:
                continue  # no tocar admins ni streamer

            motivo = None

            # 1️⃣ Sin vinculación de Twitch
            if not m.get("twitch_id"):
                motivo = "no vinculado"
                sin_vinculo += 1

            # 2️⃣ Verificar suscripción real en Twitch
            elif b.get("broadcaster_id") and access_token:
                try:
                    resp = requests.get(
                        "https://api.twitch.tv/helix/subscriptions/user",
                        headers=headers_twitch,
                        params={
                            "broadcaster_id": b["broadcaster_id"],
                            "user_id": m["twitch_id"],
                        },
                        timeout=10
                    )
                    data = resp.json().get("data", [])
                    is_still_sub = len(data) > 0

                    # Actualizar estado en BD
                    await db.exec(
                        "UPDATE telegram_users SET is_subscribed=%s WHERE telegram_id=%s",
                        is_still_sub, tg_id
                    )
                    await db.exec(
                        "UPDATE group_members SET is_subscribed=%s WHERE telegram_id=%s AND group_id=%s",
                        is_still_sub, tg_id, group_id
                    )

                    if not is_still_sub:
                        motivo = "ya no es sub"

                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ Error verificando Twitch para {m.get('username')}: {e}")

            # 3️⃣ Inactividad prolongada (>30 días)
            elif m.get("joined_at") and (ahora - m["joined_at"]).days > 30:
                motivo = "inactivo"

            # ────────────────────────────────
            # 🚫 Expulsión si aplica
            # ────────────────────────────────
            if motivo:
                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    await bot.unban_chat_member(group_id, tg_id)
                    expulsados += 1
                    logging.info(f"🚫 Expulsado @{m.get('username')} ({tg_id}) — {motivo}")

                    # 🧹 Eliminar de BD
                    await db.exec(
                        "DELETE FROM group_members WHERE group_id=%s AND telegram_id=%s",
                        group_id, tg_id
                    )

                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar {tg_id}: {e}")

        # ────────────────────────────────
        # 🔹 Registrar auditoría
        # ────────────────────────────────
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

            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
                "📊 Abrir Dashboard",
                url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}"
            )]])

            await update.message.reply_text("✅ Auditoría completada. Revisa tu Dashboard.", reply_markup=keyboard)

        except Exception as e:
            logging.error(f"Error en auditoría manual ({slug}): {e}")
            await update.message.reply_text("⚠️ Error al ejecutar la auditoría.")

    def schedule_weekly_audit(self, job_queue):
        """Programa la auditoría automática los lunes a las 09:00 UTC."""
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
                time=dtime(hour=9, minute=0),
                days=(0,),  # Lunes
                name="weekly_full_audit"
            )
            logging.info("🗓️ Auditoría semanal programada: cada lunes a las 09:00 UTC")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo programar la auditoría semanal: {e}")
