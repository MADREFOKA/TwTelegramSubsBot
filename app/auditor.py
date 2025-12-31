# app/auditor.py
import logging
import requests
import asyncio
from datetime import datetime, timedelta, time as dtime

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database

# =====================================================================
#                    HELPER: LISTA COMPLETA DE SUBS
# =====================================================================

def fetch_all_subs(broadcaster_id, headers):
    """
    Devuelve (subs_set, err)
      - subs_set: set(str(user_id)) si OK
      - err: (status_code, resp_text) si falla
    """
    url = "https://api.twitch.tv/helix/subscriptions"
    subs = set()
    cursor = None

    while True:
        params = {"broadcaster_id": broadcaster_id, "first": 100}
        if cursor:
            params["after"] = cursor

        resp = requests.get(url, headers=headers, params=params, timeout=15)

        if resp.status_code != 200:
            return None, (resp.status_code, resp.text)

        payload = resp.json()
        for s in payload.get("data", []):
            # Helix devuelve user_id del sub
            subs.add(str(s["user_id"]))

        cursor = payload.get("pagination", {}).get("cursor")
        if not cursor:
            break

    return subs, None


# =====================================================================
#                           AUDITORÍA COMPLETA
# =====================================================================

async def run_full_audit(bot, db: Database, cfg, slug: str, refresh_twitch_token):
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

        # Miembros actuales
        members = await db.all("""
            SELECT telegram_id, username, twitch_id, twitch_login, is_subscribed
            FROM group_members
            WHERE group_id=%s
        """, group_id)

        # Admins / privilegiados
        admins = await bot.get_chat_administrators(group_id)
        admin_ids = {a.user.id for a in admins}

        # privileged users = NO expulsables
        raw_privileged = await db.get_privileged_ids(slug)
        privileged_ids = set()
        for x in raw_privileged or []:
            try:
                privileged_ids.add(int(x))
            except Exception:
                pass

        total = len(members)

        # Crear log de auditoría
        row = await db.one("""
            INSERT INTO audit_logs (broadcaster_id, group_id, streamer_slug)
            VALUES (%s,%s,%s)
            RETURNING id
        """, broadcaster_id, group_id, slug)
        audit_id = row["id"]

        # ===============================================================
        # Token Twitch + OBTENER LISTA COMPLETA DE SUBS (modo seguro)
        # Si hay cualquier error -> NO expulsar a nadie (abort audit)
        # ===============================================================
        access_token = await refresh_twitch_token(b)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": cfg.client_id,
        }

        subs_set, err = fetch_all_subs(broadcaster_id, headers)

        # Si Twitch responde 401, refrescar SOLO UNA VEZ y reintentar
        if err and err[0] == 401:
            access_token = await refresh_twitch_token(b)
            headers["Authorization"] = f"Bearer {access_token}"
            subs_set, err = fetch_all_subs(broadcaster_id, headers)

        # Si SIGUE habiendo error -> auditoría inválida: no expulsar a nadie
        if err:
            fallos += 1
            status, body = err

            logging.warning(f"⛔ Auditoría abortada: Twitch ERROR {status} al obtener subs: {body[:300]}")

            await db.exec("""
                INSERT INTO audit_details
                (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                VALUES (%s,NULL,NULL,NULL,NULL,'fallo',%s)
            """, audit_id, f"Auditoría cancelada (modo seguro): no se pudo obtener lista de subs. Twitch {status}: {body[:300]}")

            await db.exec("""
                UPDATE audit_logs
                SET total_miembros=%s, expulsados=%s, fallos=%s, fecha_auditoria=NOW()
                WHERE id=%s
            """, total, 0, fallos, audit_id)

            return

        expulsados_total = 0

        for m in members:
            tg_id = int(m["telegram_id"])

            # NO expulsar admins o privileged_users
            if tg_id in admin_ids or tg_id in privileged_ids:
                continue

            # Si twitch_id falta/NULL/vacío → expulsar también (modo: sin Twitch ID)
            if not m.get("twitch_id"):
                motivo = "sin twitch_id (NULL/vacío) en group_members"

                # Registrar expulsado (aunque no haya twitch_id)
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'expulsado',%s)
                """, audit_id, tg_id, m.get("username"), m.get("twitch_login"), None, motivo)

                # Expulsión
                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    try:
                        await bot.unban_chat_member(group_id, tg_id)
                    except Exception:
                        pass
                except Exception as e:
                    fallos += 1
                    await db.exec("""
                        INSERT INTO audit_details
                        (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                        VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                    """, audit_id, tg_id, m.get("username"), m.get("twitch_login"),
                       None, f"Error al expulsar: {e}")
                    continue

                # Marcar usuario como no sub
                await db.exec("""
                    UPDATE telegram_users
                    SET is_subscribed=FALSE
                    WHERE telegram_id=%s AND streamer_id=%s
                """, tg_id, broadcaster_id)

                # Eliminar de group_members
                await db.exec("""
                    DELETE FROM group_members
                    WHERE group_id=%s AND telegram_id=%s AND broadcaster_id=%s
                """, group_id, tg_id, broadcaster_id)

                expulsados += 1
                expulsados_total += 1

                await asyncio.sleep(0.2)
                if expulsados_total % 50 == 0:
                    await asyncio.sleep(15)

                continue

            try:
                still_sub = str(m["twitch_id"]) in subs_set

                # Actualizar is_subscribed 
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

                # Si sigue siendo sub → nada más
                if still_sub:
                    continue

                # Registrar expulsado
                motivo = "ya no es sub"
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'expulsado',%s)
                """, audit_id, tg_id, m["username"], m.get("twitch_login"), m["twitch_id"], motivo)

                # Expulsión
                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    try:
                        await bot.unban_chat_member(group_id, tg_id)
                    except Exception:
                        pass
                except Exception as e:
                    fallos += 1
                    await db.exec("""
                        INSERT INTO audit_details
                        (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                        VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                    """, audit_id, tg_id, m["username"], m.get("twitch_login"),
                       m["twitch_id"], f"Error al expulsar: {e}")
                    continue

                # Eliminar de group_members
                await db.exec("""
                    DELETE FROM group_members
                    WHERE group_id=%s AND telegram_id=%s AND broadcaster_id=%s
                """, group_id, tg_id, broadcaster_id)

                expulsados += 1
                expulsados_total += 1

                await asyncio.sleep(0.2)
                if expulsados_total % 50 == 0:
                    await asyncio.sleep(15)

            except Exception as e:
                fallos += 1
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                """, audit_id, tg_id, m["username"], m.get("twitch_login"),
                   m.get("twitch_id"), str(e))

        # Guardar totales
        await db.exec("""
            UPDATE audit_logs
            SET total_miembros=%s, expulsados=%s, fallos=%s, fecha_auditoria=NOW()
            WHERE id=%s
        """, total, expulsados, fallos, audit_id)

    except Exception as e:
        logging.exception(f"❌ Error en run_full_audit({slug}): {e}")

    finally:
        if audit_id is not None:
            await db.exec("""
                UPDATE audit_logs
                SET total_miembros = COALESCE(total_miembros, %s),
                    expulsados = COALESCE(expulsados, %s),
                    fallos = COALESCE(fallos, %s),
                    fecha_auditoria = COALESCE(fecha_auditoria, NOW())
                WHERE id=%s
            """, total or 0, expulsados or 0, fallos or 0, audit_id)

# =====================================================================
#                           AUDITOR CLASS
# =====================================================================

class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token


    # Enviar mensajes largos
    async def send_long_message(self, bot, chat_id, text, disable_web_page_preview=True):
        MAX_LEN = 4000
        parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
        for part in parts:
            await bot.send_message(chat_id, part, disable_web_page_preview=disable_web_page_preview)

    
    # =================================================================
    #                           /auditnow
    # =================================================================

    async def cmd_auditnow(self, update, context):
        user = update.effective_user
        chat = update.effective_chat
        args = context.args if hasattr(context, "args") else []

        # Debe ser en privado
        if chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado conmigo.")

        # 🔥 SUPERADMIN → puede auditar cualquier canal
        if user.id == self.cfg.bot_owner_id:
            if not args:
                return await update.message.reply_text("Usa: /auditnow <slug>")
            slug_arg = args[0].lower()
            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug_arg)
            if not b:
                return await update.message.reply_text("❌ Slug no encontrado.")
            slug = b["slug"]

        else:
            # Solo streamer real
            b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
            if not b:
                return await update.message.reply_text("❌ No estás autorizado para usar /auditnow.")
            slug = b["slug"]
            
        await update.message.reply_text(f"⏳ Iniciando auditoría para '{slug}'...")
        
        chat_id = chat.id
        bot = context.bot
        
        dashboard_url = f"{self.cfg.base_url}/dashboard/{slug}?token={b['dashboard_token']}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Abrir Dashboard", url=dashboard_url)
        ]])
        
        async def _do_audit():
            try:
                await run_full_audit(bot, self.db, self.cfg, slug, self.refresh_twitch_token)
                await bot.send_message(chat_id, "✅ Auditoría completada.", reply_markup=keyboard)
            except Exception as e:
                logging.exception(f"❌ Error en auditoría background ({slug}): {e}")
                await bot.send_message(chat_id, "❌ Error ejecutando la auditoría. Mira logs / dashboard.")
        
        # ✅ IMPORTANTE: no bloquear el comando (evita reintentos del webhook)
        context.application.create_task(_do_audit())
        return


    # =================================================================
    #                           /auditcheck
    # =================================================================

    async def cmd_auditcheck(self, update, context):
        user = update.effective_user
        chat = update.effective_chat

        # 🔥 SUPERADMIN → acceso total
        if user.id == self.cfg.bot_owner_id:
            if not context.args:
                return await update.message.reply_text("Usa: /auditcheck <slug>")
            slug = context.args[0].lower()
            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
            if not b:
                return await update.message.reply_text("❌ Slug no encontrado.")

        elif chat.type in ("group", "supergroup"):
            # Admins del grupo pueden usarlo dentro del grupo
            member = await context.bot.get_chat_member(chat.id, user.id)
            if member.status not in ("administrator", "creator"):
                return await update.message.reply_text("⚠️ Solo administradores del grupo pueden usar /auditcheck.")

            b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat.id)
            if not b:
                return await update.message.reply_text("❌ Este grupo no está vinculado a ningún canal.")
            slug = b["slug"]

        else:
            # En privado → solo streamer real
            b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
            if not b:
                return await update.message.reply_text("❌ No estás autorizado a usar /auditcheck.")
            slug = b["slug"]

        result_text = await self.check_subs_only(slug)
        await self.send_long_message(context.bot, chat.id, result_text)



    # =================================================================
    #                        FUNCIÓN DE CHECK SEGURO
    # =================================================================

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

        text = f"📋 *Comprobación de subs para '{slug}' (lista completa, modo seguro)*\n\n"

        # Obtener lista completa de subs (1 refresh si 401)
        subs_set, err = fetch_all_subs(broadcaster_id, headers)

        if err and err[0] == 401:
            access_token = await self.refresh_twitch_token(b)
            headers["Authorization"] = f"Bearer {access_token}"
            subs_set, err = fetch_all_subs(broadcaster_id, headers)

        # Si falla -> NO tocar nada, solo informar
        if err:
            status, body = err
            text += "⚠️ *No se pudo obtener la lista de suscriptores de Twitch.*\n"
            text += f"Motivo: `HTTP {status}`\n"
            text += "➡️ Resultado: *no se ha tomado ninguna acción* (modo seguro).\n"
            return text

        ok = no = sin = 0

        for m in members:
            try:
                # Si no tiene twitch_id, no se puede comprobar
                if not m.get("twitch_id"):
                    sin += 1
                    text += f"⚠️ @{m.get('username') or m['telegram_id']} → SIN TWITCH_ID\n"
                    continue

                still_sub = str(m["twitch_id"]) in subs_set
                if still_sub:
                    ok += 1
                else:
                    no += 1

                text += f"• @{m.get('username') or m['telegram_id']} → {'🟢 SUB' if still_sub else '🔴 NO SUB'}\n"

            except Exception as e:
                text += f"⚠️ @{m.get('username') or m['telegram_id']} → Error: {str(e)}\n"

        text += f"\nResumen: 🟢 {ok} | 🔴 {no} | ⚠️ {sin}\n"
        return text
