# app/auditor.py
import logging
import datetime
import requests
import asyncio
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database
from datetime import time as dtime


# ──────────────────────────────────────────────
# 🔍 AUDITORÍA COMPLETA — Expulsa no subs, no vinculados e inactivos
# ──────────────────────────────────────────────
async def run_full_audit(bot, db: Database, slug: str, cfg=None, refresh_twitch_token=None):
    """
    Ejecuta una auditoría completa:
    - Expulsa usuarios no vinculados, no suscriptores o inactivos.
    - Expulsa también usuarios que hayan hablado en el grupo pero no estén en group_members ni privilegiados.
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

        # Obtener miembros y privilegiados
        members = await db.all("""
            SELECT telegram_id, username, linked_twitch_id, twitch_login, is_subscribed, joined_at
            FROM group_members
            WHERE group_id=%s
        """, group_id)
        privileged = set(await db.get_privileged_ids(slug))
        privileged_ids = privileged | {b["owner_telegram_id"]}
        db_member_ids = {m["telegram_id"] for m in members}

        total = len(members)
        expulsados = 0
        sin_vinculo = 0
        fallos = 0
        ahora = datetime.datetime.utcnow()

        # Refrescar token del broadcaster (una sola vez)
        b_access = None
        if refresh_twitch_token and cfg:
            b_access = await refresh_twitch_token(b)

        headers_b = {
            "Authorization": f"Bearer {b_access}" if b_access else "",
            "Client-Id": cfg.client_id if cfg else "",
        }

        # ────────────── Paso 1 — Comprobar miembros en BD ──────────────
        for m in members:
            tg_id = m["telegram_id"]
            if tg_id in privileged_ids:
                continue

            motivo = None
            linked_twitch_id = m.get("linked_twitch_id")
            if not linked_twitch_id:
                motivo = "no vinculado"
                sin_vinculo += 1
            else:
                # Consultar estado real en Twitch
                try:
                    r = requests.get(
                        f"{cfg.TW_API}/subscriptions/user",
                        headers=headers_b,
                        params={"broadcaster_id": broadcaster_id, "user_id": linked_twitch_id},
                        timeout=10
                    )
                    res_json = r.json()
                    sigue_sub = bool(res_json.get("data"))
                    await db.exec(
                        "UPDATE group_members SET is_subscribed=%s WHERE telegram_id=%s",
                        sigue_sub, tg_id
                    )
                    if not sigue_sub:
                        motivo = "no sub"
                except Exception as e:
                    logging.warning(f"⚠️ No se pudo verificar sub de {m.get('username')} ({tg_id}): {e}")
                    fallos += 1

            # Inactividad (>30 días)
            if not motivo and m.get("joined_at"):
                try:
                    dias = (ahora - m["joined_at"]).days
                    if dias > 30:
                        motivo = f"inactivo ({dias}d)"
                except Exception:
                    pass

            # Expulsión si aplica
            if motivo:
                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    await bot.unban_chat_member(group_id, tg_id)
                    await db.exec("DELETE FROM group_members WHERE telegram_id=%s", tg_id)
                    expulsados += 1
                    logging.info(f"🚫 Expulsado @{m.get('username')} ({tg_id}) — {motivo}")
                    await asyncio.sleep(1.2)  # ⏳ Anti rate-limit
                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar {tg_id}: {e}")

        # ────────────── Paso 2 — Revisar usuarios que han hablado ──────────────
        try:
            async for msg in bot.get_chat_history(chat_id=group_id, limit=2000):
                user = msg.from_user
                if not user:
                    continue
                if user.id in privileged_ids or user.id in db_member_ids:
                    continue  # Ya está en BD o es privilegiado

                # Usuario fuera de la BD → expulsar
                try:
                    await bot.ban_chat_member(group_id, user.id)
                    await bot.unban_chat_member(group_id, user.id)
                    expulsados += 1
                    logging.info(f"🚫 Expulsado no registrado @{user.username} ({user.id})")
                    await asyncio.sleep(1.2)
                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar no registrado {user.id}: {e}")
        except Exception as e:
            logging.warning(f"⚠️ No se pudieron analizar mensajes recientes: {e}")

        # Registrar resultados
        await db.exec("""
            INSERT INTO audit_logs (
                broadcaster_id, group_id, etiqueta_semana,
                total_miembros, expulsados, sin_vinculo, fallos, streamer_slug
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
            broadcaster_id, group_id, datetime.date.today().strftime("Semana %W"),
            total, expulsados, sin_vinculo, fallos, slug
        )

        logging.info(f"✅ Auditoría completada para {slug}: {expulsados} expulsados (fallos: {fallos}).")

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
        """Ejecuta una auditoría completa del grupo manualmente."""
        user = update.effective_user
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")
        if not b.get("group_id"):
            return await update.message.reply_text("⚠️ No tienes grupo premium vinculado.")

        slug = b["slug"]
        try:
            await run_full_audit(request.app["bot"], self.db, b["slug"], self.cfg, self.refresh_twitch_token)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Abrir Dashboard",
                url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}")]])
            await update.message.reply_text("✅ Auditoría completada. Revisa tu Dashboard.", reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Error en auditoría manual ({slug}): {e}")
            await update.message.reply_text("⚠️ Error al ejecutar la auditoría.")

    def schedule_weekly_audit(self, job_queue):
        """Programa la auditoría automática cada lunes a las 09:00 UTC."""
        from telegram.ext import CallbackContext

        async def _weekly_task(context: 'CallbackContext'):
            rows = await self.db.all("SELECT slug FROM broadcasters")
            for r in rows:
                try:
                    await run_full_audit(context.bot, self.db, r["slug"], self.cfg, self.refresh_twitch_token)
                    await asyncio.sleep(2)  # Espera entre grupos
                except Exception as e:
                    logging.error(f"Error auditoría semanal {r['slug']}: {e}")

        try:
            job_queue.run_daily(
                _weekly_task,
                time=dtime(hour=7, minute=0),  # 09:00 UTC
                days=(0,), 
                name="weekly_full_audit"
            )
            logging.info("🗓️ Auditoría semanal programada: cada lunes a las 07:00 UTC")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo programar la auditoría semanal: {e}")
