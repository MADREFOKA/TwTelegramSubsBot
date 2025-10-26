# app/auditor.py
import logging
import datetime
import requests
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import time as dtime
from app.db import Database

# ──────────────────────────────────────────────
# 🔍 AUDITORÍA COMPLETA FINAL
# ──────────────────────────────────────────────
async def run_full_audit(bot, db: Database, slug: str, refresh_twitch_token):
    """
    Auditoría completa del grupo premium de un streamer.
    0️⃣ Escanea últimos mensajes → expulsa usuarios activos no registrados.
    1️⃣ Verifica en Twitch si cada miembro sigue siendo sub.
    2️⃣ Expulsa no subs, no vinculados o inactivos.
    3️⃣ Limpia la base de datos y registra resultados.
    """
    try:
        b = await db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or not b.get("group_id"):
            logging.warning(f"⏭️ Saltando auditoría: {slug} sin grupo vinculado.")
            return

        group_id = b["group_id"]
        broadcaster_id = b["broadcaster_id"]
        logging.info(f"🔎 Iniciando auditoría completa para {slug} ({group_id})")

        # ────────────────────────────────
        # Paso 0 — Expulsar activos no registrados
        # ────────────────────────────────
        expulsados_historial = 0
        try:
            logging.info(f"🧭 Escaneando últimos 1000 mensajes en {group_id}...")
            recent_messages = await bot.get_chat_history(group_id, limit=1000)
            active_users = {msg.from_user.id for msg in recent_messages if msg.from_user}
            logging.info(f"📌 Detectados {len(active_users)} usuarios activos recientes.")

            registered = await db.all("SELECT telegram_id FROM group_members WHERE group_id=%s", group_id)
            registered_ids = {r["telegram_id"] for r in registered}

            admins = await bot.get_chat_administrators(group_id)
            admin_ids = {adm.user.id for adm in admins}
            privileged_ids = set(await db.get_privileged_ids(slug)) | {b["owner_telegram_id"]}

            for uid in active_users:
                if uid in registered_ids or uid in admin_ids or uid in privileged_ids:
                    continue
                try:
                    await bot.ban_chat_member(group_id, uid)
                    await bot.unban_chat_member(group_id, uid)
                    expulsados_historial += 1
                    logging.info(f"🚫 Expulsado {uid} — activo en el chat pero no registrado.")
                except Exception as e:
                    logging.warning(f"⚠️ No se pudo expulsar {uid}: {e}")

            logging.info(f"📜 Expulsados por historial: {expulsados_historial}")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo leer historial o expulsar no registrados: {e}")

        # ────────────────────────────────
        # Paso 1 — Cargar miembros registrados
        # ────────────────────────────────
        members = await db.all("""
            SELECT telegram_id, username, twitch_login, is_subscribed, joined_at
            FROM group_members
            WHERE group_id=%s
        """, group_id)

        privileged_ids = set(await db.get_privileged_ids(slug)) | {b["owner_telegram_id"]}
        admins = await bot.get_chat_administrators(group_id)
        admin_ids = {adm.user.id for adm in admins}

        total = len(members)
        expulsados = 0
        sin_vinculo = 0
        fallos = 0
        ahora = datetime.datetime.utcnow()

        # ────────────────────────────────
        # Paso 2 — Preparar token de Twitch
        # ────────────────────────────────
        from app.security import TokenCipher
        cipher = TokenCipher()
        try:
            access_token = cipher.decrypt(b["access_token"])
        except Exception:
            logging.warning(f"⚠️ Token corrupto para {slug}, intentando refrescar...")
            access_token = await refresh_twitch_token(b)

        headers_twitch = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": b.get("client_id") or "",
        }

        # ────────────────────────────────
        # Paso 3 — Revisar miembros y comprobar subs
        # ────────────────────────────────
        for m in members:
            tg_id = m["telegram_id"]

            # Saltar admins o privilegiados
            if tg_id in privileged_ids or tg_id in admin_ids:
                continue

            motivo = None

            # 1️⃣ Sin vinculación Twitch
            twitch_id = m.get("twitch_login")
            if not twitch_id:
                motivo = "no vinculado a Twitch"
                sin_vinculo += 1

            # 2️⃣ Verificar si sigue siendo sub
            elif broadcaster_id:
                try:
                    resp = requests.get(
                        "https://api.twitch.tv/helix/subscriptions/user",
                        headers=headers_twitch,
                        params={
                            "broadcaster_id": broadcaster_id,
                            "user_id": twitch_id,
                        },
                        timeout=10
                    )

                    if resp.status_code == 401:
                        logging.warning(f"🔑 Token expirado, refrescando para {slug}...")
                        access_token = await refresh_twitch_token(b)
                        headers_twitch["Authorization"] = f"Bearer {access_token}"
                        resp = requests.get(
                            "https://api.twitch.tv/helix/subscriptions/user",
                            headers=headers_twitch,
                            params={
                                "broadcaster_id": broadcaster_id,
                                "user_id": twitch_id,
                            },
                            timeout=10
                        )

                    data = resp.json().get("data", [])
                    is_still_sub = len(data) > 0

                    # Actualizar estado
                    await db.exec(
                        "UPDATE telegram_users SET is_subscribed=%s WHERE telegram_id=%s",
                        is_still_sub, tg_id
                    )
                    await db.exec(
                        "UPDATE group_members SET is_subscribed=%s WHERE telegram_id=%s AND group_id=%s",
                        is_still_sub, tg_id, group_id
                    )

                    if not is_still_sub:
                        motivo = "ya no es sub activo"

                except Exception as e:
                    logging.warning(f"⚠️ Error verificando sub @{m.get('username')}: {e}")

            # 3️⃣ Inactivo (más de 30 días)
            elif m.get("joined_at") and (ahora - m["joined_at"]).days > 30:
                motivo = "inactivo"

            # ────────────────────────────────
            # Paso 4 — Expulsar si aplica
            # ────────────────────────────────
            if motivo:
                try:
                    chat_member = await bot.get_chat_member(group_id, tg_id)
                    if chat_member.status not in ("creator", "administrator", "member"):
                        await db.exec(
                            "DELETE FROM group_members WHERE group_id=%s AND telegram_id=%s",
                            group_id, tg_id
                        )
                        continue

                    await bot.ban_chat_member(group_id, tg_id)
                    await bot.unban_chat_member(group_id, tg_id)
                    expulsados += 1
                    logging.info(f"🚫 Expulsado @{m.get('username')} ({tg_id}) — {motivo}")

                    await db.exec(
                        "DELETE FROM group_members WHERE group_id=%s AND telegram_id=%s",
                        group_id, tg_id
                    )
                    await db.exec(
                        "UPDATE telegram_users SET is_subscribed=false WHERE telegram_id=%s",
                        tg_id
                    )

                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar {tg_id}: {e}")

        # ────────────────────────────────
        # Paso 5 — Registrar auditoría
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

        logging.info(f"✅ Auditoría completada para {slug}: expulsados={expulsados}, historial={expulsados_historial}.")

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
        """Ejecuta una auditoría completa del grupo al instante."""
        user = update.effective_user
        chat_type = update.effective_chat.type

        if chat_type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        b = await self.db.one(
            "SELECT * FROM broadcasters WHERE owner_telegram_id=%s",
            user.id
        )
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        slug = b["slug"]
        group_id = b.get("group_id")
        if not group_id:
            return await update.message.reply_text("⚠️ No tienes ningún grupo premium vinculado.")

        try:
            await run_full_audit(context.bot, self.db, slug, self.refresh_twitch_token)

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Abrir Dashboard",
                url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}")
            ]])

            await update.message.reply_text("✅ Auditoría completada. Revisa tu Dashboard.", reply_markup=keyboard)

        except Exception as e:
            logging.error(f"Error en auditoría manual ({slug}): {e}")
            await update.message.reply_text("⚠️ Error al ejecutar la auditoría.")

    def schedule_weekly_audit(self, job_queue):
        """Programa la auditoría automática los lunes a las 07:00 UTC."""
        from telegram.ext import CallbackContext

        async def _weekly_task(context: 'CallbackContext'):
            rows = await self.db.all("SELECT slug FROM broadcasters")
            for r in rows:
                try:
                    await run_full_audit(context.bot, self.db, r["slug"], self.refresh_twitch_token)
                except Exception as e:
                    logging.error(f"Error auditoría semanal {r['slug']}: {e}")

        try:
            job_queue.run_daily(
                _weekly_task,
                time=dtime(hour=7, minute=0),  # Lunes 07:00 UTC
                days=(0,),  # 0 = Monday
                name="weekly_full_audit"
            )
            logging.info("🗓️ Auditoría semanal programada: lunes 07:00 UTC.")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo programar la auditoría semanal: {e}")
