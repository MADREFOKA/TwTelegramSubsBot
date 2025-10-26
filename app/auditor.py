# app/auditor.py
import logging
import datetime
import requests
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import time as dtime
from app.db import Database
from app.security import TokenCipher

async def run_full_audit(bot, db: Database, slug: str, refresh_twitch_token):
    try:
        b = await db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or not b.get("group_id"):
            logging.warning(f"⏭️ Saltando auditoría: {slug} sin grupo vinculado.")
            return

        group_id = b["group_id"]
        broadcaster_id = b["broadcaster_id"]

        logging.info(f"🔎 Iniciando auditoría para {slug} ({group_id})")

        members = await db.all("""
            SELECT telegram_id, username, twitch_login, is_subscribed
            FROM group_members
            WHERE group_id=%s
        """, group_id)

        admins = await bot.get_chat_administrators(group_id)
        admin_ids = {a.user.id for a in admins}
        privileged_ids = set(await db.get_privileged_ids(slug)) | {b["owner_telegram_id"]}

        expulsados, fallos = 0, 0
        total = len(members)

        # 🔹 Crear registro vacío de auditoría (para obtener ID)
        row = await db.one("""
            INSERT INTO audit_logs (broadcaster_id, group_id, streamer_slug)
            VALUES (%s,%s,%s)
            RETURNING id
        """, broadcaster_id, group_id, slug)
        audit_id = row["id"]

        # 🔹 Verificar subs y expulsar si no lo son
        access_token = await refresh_twitch_token(b)
        headers = {"Authorization": f"Bearer {access_token}", "Client-Id": b["client_id"]}

        for m in members:
            tg_id = m["telegram_id"]
            if tg_id in privileged_ids or tg_id in admin_ids:
                continue

            try:
                resp = requests.get(
                    "https://api.twitch.tv/helix/subscriptions/user",
                    headers=headers,
                    params={"broadcaster_id": broadcaster_id, "user_id": m["twitch_login"]},
                    timeout=10
                )
                data = resp.json().get("data", [])
                still_sub = len(data) > 0

                # 🔸 Actualizar estado local
                await db.exec("UPDATE group_members SET is_subscribed=%s WHERE telegram_id=%s", still_sub, tg_id)

                if not still_sub:
                    expulsados += 1
                    motivo = "ya no es sub"
                    await db.exec("INSERT INTO audit_details (audit_id, telegram_id, username, twitch_login, accion, motivo) VALUES (%s,%s,%s,%s,'expulsado',%s)",
                                  audit_id, tg_id, m["username"], m["twitch_login"], motivo)
                    await bot.ban_chat_member(group_id, tg_id)
                    await bot.unban_chat_member(group_id, tg_id)
                    await db.exec("DELETE FROM group_members WHERE group_id=%s AND telegram_id=%s", group_id, tg_id)

            except Exception as e:
                fallos += 1
                await db.exec("INSERT INTO audit_details (audit_id, telegram_id, username, twitch_login, accion, motivo) VALUES (%s,%s,%s,%s,'fallo',%s)",
                              audit_id, tg_id, m["username"], m["twitch_login"], str(e))
                logging.warning(f"⚠️ Error verificando {m['username']}: {e}")

        # 🔹 Actualizar totales en audit_logs
        await db.exec("UPDATE audit_logs SET total_miembros=%s, expulsados=%s, fallos=%s, fecha_auditoria=NOW() WHERE id=%s",
                      total, expulsados, fallos, audit_id)

        logging.info(f"✅ Auditoría completada para {slug}: expulsados={expulsados}, fallos={fallos}")

    except Exception as e:
        logging.exception(f"❌ Error en run_full_audit({slug}): {e}")

class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    async def cmd_auditnow(self, update, context):
        user = update.effective_user
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado.")

        slug = b["slug"]
        await run_full_audit(context.bot, self.db, slug, self.refresh_twitch_token)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Abrir Dashboard",
            url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}")
        ]])
        await update.message.reply_text("✅ Auditoría completada. Revisa tu Dashboard.", reply_markup=keyboard)

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

        job_queue.run_daily(
            _weekly_task,
            time=dtime(hour=7, minute=0),
            days=(0,),
            name="weekly_full_audit"
        )
        logging.info("🗓️ Auditoría semanal programada: lunes 07:00 UTC.")
