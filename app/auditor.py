# app/auditor.py
import logging
import requests
import asyncio
from datetime import datetime, timedelta, time as dtime

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database


async def run_full_audit(bot, db: Database, cfg, slug: str, refresh_twitch_token):
    """
    Auditoría completa para un broadcaster (por slug):

    - Recorre group_members (solo gente actualmente en el grupo).
    - Comprueba en Twitch si siguen siendo subs.
    - Si NO son subs:
        - Ban REAL en el grupo de Telegram.
        - Marca is_banned = TRUE en telegram_users (para ese streamer).
        - Borra el registro de group_members (solo queremos miembros activos).
        - Inserta detalle en audit_details.
    - Actualiza audit_logs con totales.
    """
    audit_id = None
    total = expulsados = fallos = 0

    try:
        b = await db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or not b.get("group_id"):
            logging.warning(f"⏭️ Saltando auditoría: {slug} sin grupo vinculado.")
            return

        group_id = b["group_id"]
        broadcaster_id = b["broadcaster_id"]

        logging.info(f"🔎 Iniciando auditoría para {slug} (group_id={group_id})")

        # Miembros actuales (según group_members)
        members = await db.all("""
            SELECT telegram_id, username, twitch_id, twitch_login, is_subscribed
            FROM group_members
            WHERE group_id=%s
        """, group_id)

        # Admins y privilegiados
        admins = await bot.get_chat_administrators(group_id)
        admin_ids = {a.user.id for a in admins}
        privileged_ids = set(await db.get_privileged_ids(slug)) | {b["owner_telegram_id"]}

        total = len(members)

        # Crear entrada en audit_logs
        row = await db.one("""
            INSERT INTO audit_logs (broadcaster_id, group_id, streamer_slug)
            VALUES (%s,%s,%s)
            RETURNING id
        """, broadcaster_id, group_id, slug)
        audit_id = row["id"]

        # Preparar token de Twitch
        access_token = await refresh_twitch_token(b)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": cfg.client_id,
        }

        expulsados_total = 0

        for m in members:
            tg_id = m["telegram_id"]

            # Saltar admins / privilegiados
            if tg_id in admin_ids or tg_id in privileged_ids:
                continue

            # ❗ FIX: evitar auditorías rotas si NO hay twitch_id
            if not m["twitch_id"]:
                fallos += 1
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'fallo','twitch_id vacío en group_members')
                """, audit_id, tg_id, m["username"], m.get("twitch_login"), None)
                logging.warning(f"⚠️ Usuario {m['username']} sin twitch_id. Saltado.")
                continue

            try:
                # Consultar estado de sub en Twitch
                resp = requests.get(
                    "https://api.twitch.tv/helix/subscriptions/user",
                    headers=headers,
                    params={"broadcaster_id": broadcaster_id, "user_id": m["twitch_id"]},
                    timeout=10
                )

                # ❗ FIX: si Twitch devuelve error → NO expulsar
                if resp.status_code != 200:
                    logging.warning(f"⚠️ Twitch ERROR {resp.status_code}: {resp.text}")
                    still_sub = m["is_subscribed"]
                else:
                    data = resp.json().get("data", [])
                    still_sub = len(data) > 0

                # ❗ FIX: GROUP MEMBERS update usando group_id (antes estaba mal)
                await db.exec("""
                    UPDATE group_members
                    SET is_subscribed=%s
                    WHERE telegram_id=%s AND group_id=%s
                """, still_sub, tg_id, group_id)

                await db.exec("""
                    UPDATE telegram_users
                    SET is_subscribed=%s
                    WHERE telegram_id=%s AND streamer_id=%s
                """, still_sub, tg_id, broadcaster_id)

                # Si sigue siendo sub, pasamos
                if still_sub:
                    continue

                # Registrar detalle expulsado
                motivo = "ya no es sub"
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'expulsado',%s)
                """, audit_id, tg_id, m["username"], m.get("twitch_login"), m["twitch_id"], motivo)

                # Ban real
                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    logging.info(f"👢 Baneado {m['username']} ({tg_id}) de {group_id}")
                except Exception as e:
                    fallos += 1
                    await db.exec("""
                        INSERT INTO audit_details
                        (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                        VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                    """, audit_id, tg_id, m["username"], m.get("twitch_login"), m["twitch_id"], f"Error al banear: {e}")
                    logging.warning(f"⚠️ Error al banear {m['username']}: {e}")
                    continue

                # Marcar is_banned
                try:
                    await db.exec("""
                        UPDATE telegram_users
                        SET is_banned = TRUE
                        WHERE telegram_id=%s AND streamer_id=%s
                    """, tg_id, broadcaster_id)
                except Exception as e:
                    logging.warning(f"⚠️ No se pudo marcar is_banned: {e}")

                # Eliminar de group_members
                await db.exec("""
                    DELETE FROM group_members
                    WHERE group_id=%s AND telegram_id=%s AND broadcaster_id=%s
                """, group_id, tg_id, broadcaster_id)

                expulsados += 1
                expulsados_total += 1

                await asyncio.sleep(0.2)
                if expulsados_total % 50 == 0:
                    logging.info("⏸️ Pausa: 50 expulsiones → 15s espera")
                    await asyncio.sleep(15)

            except Exception as e:
                fallos += 1
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                """, audit_id, tg_id, m["username"], m.get("twitch_login"), m.get("twitch_id"), str(e))
                logging.warning(f"⚠️ Error verificando {m['username']}: {e}")

        # Finalizar totales
        await db.exec("""
            UPDATE audit_logs
            SET total_miembros=%s, expulsados=%s, fallos=%s, fecha_auditoria=NOW()
            WHERE id=%s
        """, total, expulsados, fallos, audit_id)

        logging.info(f"✅ Auditoría completada para {slug}")

    except Exception as e:
        logging.exception(f"❌ Error en run_full_audit({slug}): {e}")

    finally:
        if audit_id is not None:
            try:
                await db.exec("""
                    UPDATE audit_logs
                    SET total_miembros = COALESCE(total_miembros, %s),
                        expulsados = COALESCE(expulsados, %s),
                        fallos = COALESCE(fallos, %s),
                        fecha_auditoria = COALESCE(fecha_auditoria, NOW())
                    WHERE id=%s
                """, total or 0, expulsados or 0, fallos or 0, audit_id)
            except Exception as e:
                logging.warning(f"⚠️ No se pudo normalizar auditoría {audit_id}")


class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    # 📌 Enviar mensajes largos SIN romper Telegram
    async def send_long_message(self, bot, chat_id, text, disable_web_page_preview=True):
        MAX_LEN = 4000
        parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
        for part in parts:
            await bot.send_message(chat_id, part, disable_web_page_preview=disable_web_page_preview)

    async def cmd_auditnow(self, update, context):
        user = update.effective_user
        chat = update.effective_chat
        args = context.args if hasattr(context, "args") else []

        # Solo en privado
        if chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado conmigo.")

        slug = None
        b = None

        # BOT_OWNER puede auditar cualquier slug
        if args and getattr(self.cfg, "bot_owner_id", None) and user.id == self.cfg.bot_owner_id:
            slug_arg = args[0].lower()
            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug_arg)
            if not b:
                return await update.message.reply_text(f"❌ No se encontró broadcaster '{slug_arg}'.")
            slug = b["slug"]
        else:
            # Owner original del canal
            b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
            if not b:
                return await update.message.reply_text("❌ No tienes canal vinculado.")
            slug = b["slug"]

        await update.message.reply_text(f"⏳ Iniciando auditoría para '{slug}'...")

        await run_full_audit(context.bot, self.db, self.cfg, slug, self.refresh_twitch_token)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📊 Abrir Dashboard",
                url=f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}"
            )
        ]])

        await update.message.reply_text("✅ Auditoría completada.", reply_markup=keyboard)

    async def cmd_auditcheck(self, update, context):
        user = update.effective_user
        chat = update.effective_chat

        # Si se ejecuta en grupo → solo admins
        if chat.type in ("group", "supergroup"):
            member = await context.bot.get_chat_member(chat.id, user.id)
            if member.status not in ("administrator", "creator"):
                return await update.message.reply_text("⚠️ Solo administradores pueden usar /auditcheck.")

            b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat.id)
            if not b:
                return await update.message.reply_text("❌ Este grupo no está vinculado a ningún canal.")

            slug = b["slug"]

        else:
            # En privado → propietario del canal
            b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
            if not b:
                return await update.message.reply_text("❌ No tienes canal vinculado.")
            slug = b["slug"]

        result_text = await self.check_subs_only(slug)
        await self.send_long_message(context.bot, chat.id, result_text)

    async def check_subs_only(self, slug):
        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        broadcaster_id = b["broadcaster_id"]
        group_id = b["group_id"]

        access_token = await self.refresh_twitch_token(b)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": self.cfg.client_id,
        }

        members = await self.db.all("""
            SELECT telegram_id, username, twitch_id, twitch_login, is_subscribed
            FROM group_members
            WHERE group_id=%s
        """, group_id)

        text = f"📋 *Comprobación de subs para '{slug}' (modo seguro)*\n\n"

        for m in members:
            try:
                resp = requests.get(
                    "https://api.twitch.tv/helix/subscriptions/user",
                    headers=headers,
                    params={"broadcaster_id": broadcaster_id, "user_id": m["twitch_id"]},
                    timeout=10
                )

                if resp.status_code != 200:
                    text += f"⚠️ @{m['username']} → ERROR {resp.status_code}\n"
                    continue

                data = resp.json().get("data", [])
                still_sub = len(data) > 0

                text += f"• @{m['username']} → {'🟢 SUB' if still_sub else '🔴 NO SUB'}\n"

            except Exception as e:
                text += f"⚠️ @{m['username']} → Error Twitch: {str(e)}\n"

        return text
