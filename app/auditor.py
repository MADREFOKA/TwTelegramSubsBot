# app/auditor.py
import logging
import requests
import asyncio
import io
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
            subs.add(str(s["user_id"]))

        cursor = payload.get("pagination", {}).get("cursor")
        if not cursor:
            break

    return subs, None


# =====================================================================
#                           AUDITORÍA COMPLETA
# =====================================================================

async def run_full_audit(bot, db: Database, cfg, slug: str, refresh_twitch_token):
    """
    🔥 AUDITORÍA MEJORADA:
    - Usa SOLO group_members (no telegram_users)
    - Requiere twitch_id obligatorio
    - Modo seguro: si falla obtener lista de Twitch → NO expulsar a nadie
    - Expulsa SOLO usuarios que ya NO son subs según Twitch
    """
    audit_id = None
    total = expulsados = fallos = 0

    try:
        b = await db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or not b.get("group_id"):
            logging.warning(f"⚠️ Saltando auditoría: {slug} sin grupo vinculado.")
            return

        group_id = b["group_id"]
        broadcaster_id = b["broadcaster_id"]

        logging.info(f"🔎 Iniciando auditoría para {slug} (group_id={group_id})")

        # 🔥 NUEVO: Obtener miembros SOLO de group_members
        members = await db.all("""
            SELECT telegram_id, username, twitch_id, twitch_login, is_subscribed
            FROM group_members
            WHERE group_id=%s AND broadcaster_id=%s
        """, group_id, broadcaster_id)

        # Admins / privilegiados
        admins = await bot.get_chat_administrators(group_id)
        admin_ids = {a.user.id for a in admins}

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
        # 🔥 OBTENER LISTA COMPLETA DE SUBS (modo seguro)
        # ===============================================================
        access_token = await refresh_twitch_token(b)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Client-Id": cfg.client_id,
        }

        subs_set, err = fetch_all_subs(broadcaster_id, headers)

        if err and err[0] == 401:
            logging.warning("⚠️ Token caducado, refrescando...")
            access_token = await refresh_twitch_token(b)
            headers["Authorization"] = f"Bearer {access_token}"
            subs_set, err = fetch_all_subs(broadcaster_id, headers)

        if err:
            fallos += 1
            status, body = err
            logging.error(f"❌ Auditoría abortada: Twitch ERROR {status}")

            await db.exec("""
                INSERT INTO audit_details
                (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                VALUES (%s,NULL,NULL,NULL,NULL,'fallo',%s)
            """, audit_id, f"Auditoría cancelada: no se pudo obtener lista de subs. Twitch {status}")

            await db.exec("""
                UPDATE audit_logs
                SET total_miembros=%s, expulsados=%s, fallos=%s, fecha_auditoria=NOW()
                WHERE id=%s
            """, total, 0, fallos, audit_id)

            logging.warning("⛔ AUDITORÍA ABORTADA: No se expulsó a nadie por seguridad")
            return

        logging.info(f"✅ Lista de subs obtenida: {len(subs_set)} suscriptores activos")

        expulsados_total = 0
        usuarios_sin_twitch_id = 0
        usuarios_aun_subs = 0

        for m in members:
            tg_id = int(m["telegram_id"])

            if tg_id in admin_ids:
                logging.info(f"⏭️ Saltando admin: {m.get('username') or tg_id}")
                continue
                
            if tg_id in privileged_ids:
                logging.info(f"⏭️ Saltando privilegiado: {m.get('username') or tg_id}")
                continue

            # 🔥 Si NO tiene twitch_id → expulsar
            if not m.get("twitch_id"):
                usuarios_sin_twitch_id += 1
                motivo = "sin twitch_id en group_members"

                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'expulsado',%s)
                """, audit_id, tg_id, m.get("username"), m.get("twitch_login"), None, motivo)

                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    await asyncio.sleep(0.3)
                    try:
                        await bot.unban_chat_member(group_id, tg_id)
                    except Exception:
                        pass
                except Exception as e:
                    fallos += 1
                    logging.error(f"❌ Error expulsando {tg_id}: {e}")
                    await db.exec("""
                        INSERT INTO audit_details
                        (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                        VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                    """, audit_id, tg_id, m.get("username"), m.get("twitch_login"),
                       None, f"Error al expulsar: {e}")
                    continue

                await db.exec("""
                    UPDATE telegram_users
                    SET is_subscribed=FALSE
                    WHERE telegram_id=%s AND streamer_id=%s
                """, tg_id, broadcaster_id)

                await db.exec("""
                    DELETE FROM group_members
                    WHERE group_id=%s AND telegram_id=%s AND broadcaster_id=%s
                """, group_id, tg_id, broadcaster_id)

                expulsados += 1
                expulsados_total += 1
                logging.info(f"🧹 Expulsado por falta de twitch_id: {m.get('username') or tg_id}")

                await asyncio.sleep(0.2)
                if expulsados_total % 50 == 0:
                    await asyncio.sleep(15)

                continue

            # 🔥 VERIFICAR SI SIGUE SIENDO SUB
            try:
                still_sub = str(m["twitch_id"]) in subs_set

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

                if still_sub:
                    usuarios_aun_subs += 1
                    continue

                # NO ES SUB → EXPULSAR
                motivo = "ya no es sub"
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'expulsado',%s)
                """, audit_id, tg_id, m["username"], m.get("twitch_login"), m["twitch_id"], motivo)

                try:
                    await bot.ban_chat_member(group_id, tg_id)
                    await asyncio.sleep(0.3)
                    try:
                        await bot.unban_chat_member(group_id, tg_id)
                    except Exception:
                        pass
                except Exception as e:
                    fallos += 1
                    logging.error(f"❌ Error expulsando {tg_id}: {e}")
                    await db.exec("""
                        INSERT INTO audit_details
                        (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                        VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                    """, audit_id, tg_id, m["username"], m.get("twitch_login"),
                       m["twitch_id"], f"Error al expulsar: {e}")
                    continue

                await db.exec("""
                    DELETE FROM group_members
                    WHERE group_id=%s AND telegram_id=%s AND broadcaster_id=%s
                """, group_id, tg_id, broadcaster_id)

                expulsados += 1
                expulsados_total += 1
                logging.info(f"🚫 Expulsado (no sub): {m.get('username') or tg_id}")

                await asyncio.sleep(0.2)
                if expulsados_total % 50 == 0:
                    await asyncio.sleep(15)

            except Exception as e:
                fallos += 1
                logging.error(f"❌ Error procesando {tg_id}: {e}")
                await db.exec("""
                    INSERT INTO audit_details
                    (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
                    VALUES (%s,%s,%s,%s,%s,'fallo',%s)
                """, audit_id, tg_id, m["username"], m.get("twitch_login"),
                   m.get("twitch_id"), str(e))

        await db.exec("""
            UPDATE audit_logs
            SET total_miembros=%s, expulsados=%s, fallos=%s, fecha_auditoria=NOW()
            WHERE id=%s
        """, total, expulsados, fallos, audit_id)

        logging.info(
            f"📊 Auditoría completada: {total} miembros, {expulsados} expulsados, "
            f"{usuarios_aun_subs} siguen siendo subs, {usuarios_sin_twitch_id} sin twitch_id, "
            f"{fallos} fallos"
        )

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

        if chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado conmigo.")

        if user.id == self.cfg.bot_owner_id:
            if not args:
                return await update.message.reply_text("Usa: /auditnow <slug>")
            slug_arg = args[0].lower()
            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug_arg)
            if not b:
                return await update.message.reply_text("❌ Slug no encontrado.")
            slug = b["slug"]
        else:
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
        
        context.application.create_task(_do_audit())
        return

    # =================================================================
    #                           /auditcheck
    # =================================================================

    async def cmd_auditcheck(self, update, context):
        user = update.effective_user
        chat = update.effective_chat

        if user.id == self.cfg.bot_owner_id:
            if not context.args:
                return await update.message.reply_text("Usa: /auditcheck <slug>")
            slug = context.args[0].lower()
            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
            if not b:
                return await update.message.reply_text("❌ Slug no encontrado.")

        elif chat.type in ("group", "supergroup"):
            member = await context.bot.get_chat_member(chat.id, user.id)
            if member.status not in ("administrator", "creator"):
                return await update.message.reply_text("⚠️ Solo administradores del grupo pueden usar /auditcheck.")

            b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat.id)
            if not b:
                return await update.message.reply_text("❌ Este grupo no está vinculado a ningún canal.")
            slug = b["slug"]

        else:
            b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
            if not b:
                return await update.message.reply_text("❌ No estás autorizado a usar /auditcheck.")
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
            WHERE group_id=%s AND broadcaster_id=%s
        """, group_id, broadcaster_id)

        text = f"📋 *Comprobación de subs para '{slug}' (grupo premium)*\n\n"

        subs_set, err = fetch_all_subs(broadcaster_id, headers)

        if err and err[0] == 401:
            access_token = await self.refresh_twitch_token(b)
            headers["Authorization"] = f"Bearer {access_token}"
            subs_set, err = fetch_all_subs(broadcaster_id, headers)

        if err:
            status, body = err
            text += "⚠️ *No se pudo obtener la lista de suscriptores de Twitch.*\n"
            text += f"Motivo: `HTTP {status}`\n"
            text += "➡️ Resultado: *no se ha tomado ninguna acción* (modo seguro).\n"
            return text

        ok = no = sin = 0

        for m in members:
            try:
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
        text += f"\nTotal en group_members: {len(members)}\n"
        return text

    # =================================================================
    #                    🔥 NUEVO: /verify
    # =================================================================

    async def cmd_verify(self, update, context):
        """
        🔍 VERIFICACIÓN COMPLETA SIN EXPULSIONES
        Obtiene lista fresca de Twitch y genera reporte detallado
        """
        user = update.effective_user
        chat = update.effective_chat

        # Permisos
        if user.id == self.cfg.bot_owner_id:
            if chat.type == "private":
                if not context.args:
                    return await update.message.reply_text("Usa: /verify <slug>")
                slug = context.args[0].lower()
                b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
                if not b:
                    return await update.message.reply_text("❌ Slug no encontrado.")
            else:
                b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat.id)
                if not b:
                    return await update.message.reply_text("❌ Este grupo no está vinculado.")
                slug = b["slug"]

        elif chat.type in ("group", "supergroup"):
            member = await context.bot.get_chat_member(chat.id, user.id)
            if member.status not in ("administrator", "creator"):
                return await update.message.reply_text("⚠️ Solo administradores pueden usar /verify.")

            b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat.id)
            if not b:
                return await update.message.reply_text("❌ Este grupo no está vinculado a ningún canal.")
            slug = b["slug"]

        else:
            b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
            if not b:
                return await update.message.reply_text("❌ No estás autorizado a usar /verify.")
            slug = b["slug"]

        msg = await update.message.reply_text(
            f"🔍 Verificando suscripciones para **{slug}**...\n⏳ Esto puede tardar unos segundos.",
            parse_mode="Markdown"
        )

        broadcaster_id = b["broadcaster_id"]
        group_id = b.get("group_id")

        if not group_id:
            return await msg.edit_text("❌ Este streamer no tiene grupo premium vinculado.")

        # Obtener lista de Twitch
        try:
            access_token = await self.refresh_twitch_token(b)
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": self.cfg.client_id,
            }

            subs_set, err = fetch_all_subs(broadcaster_id, headers)

            if err and err[0] == 401:
                access_token = await self.refresh_twitch_token(b)
                headers["Authorization"] = f"Bearer {access_token}"
                subs_set, err = fetch_all_subs(broadcaster_id, headers)

            if err:
                status, body = err
                return await msg.edit_text(
                    f"❌ **Error al obtener subs de Twitch**\n\n"
                    f"Código: `HTTP {status}`\n"
                    f"Detalle: `{body[:200]}`",
                    parse_mode="Markdown"
                )

        except Exception as e:
            logging.exception("Error obteniendo lista de subs")
            return await msg.edit_text(f"❌ **Error**: `{str(e)}`", parse_mode="Markdown")

        # Obtener miembros
        members = await self.db.all("""
            SELECT telegram_id, username, twitch_id, twitch_login, is_subscribed
            FROM group_members
            WHERE group_id=%s AND broadcaster_id=%s
            ORDER BY username
        """, group_id, broadcaster_id)

        if not members:
            return await msg.edit_text(
                f"📭 No hay miembros en `group_members` para {slug}.\n\n"
                f"Ejecuta /sync_members para sincronizar.",
                parse_mode="Markdown"
            )

        # Obtener protegidos
        try:
            admins = await context.bot.get_chat_administrators(group_id)
            admin_ids = {a.user.id for a in admins}
        except Exception:
            admin_ids = set()

        raw_privileged = await self.db.get_privileged_ids(slug)
        privileged_ids = {int(x) for x in raw_privileged or [] if x}

        # Analizar
        results = {
            "subs_ok": [],
            "not_subs": [],
            "no_twitch_id": [],
            "admins": [],
            "privileged": [],
            "not_in_group": []
        }

        for m in members:
            tg_id = int(m["telegram_id"])
            username = m.get("username") or f"ID:{tg_id}"
            twitch_id = m.get("twitch_id")
            twitch_login = m.get("twitch_login") or "desconocido"

            try:
                member_status = await context.bot.get_chat_member(group_id, tg_id)
                is_in_group = member_status.status in ("member", "administrator", "creator", "restricted")
            except Exception:
                is_in_group = False

            if not is_in_group:
                results["not_in_group"].append({"username": username, "twitch_login": twitch_login})
                continue

            if tg_id in admin_ids:
                results["admins"].append({"username": username, "twitch_login": twitch_login})
                continue

            if tg_id in privileged_ids:
                results["privileged"].append({"username": username, "twitch_login": twitch_login})
                continue

            if not twitch_id:
                results["no_twitch_id"].append({"username": username, "telegram_id": tg_id})
                continue

            is_sub = str(twitch_id) in subs_set

            if is_sub:
                results["subs_ok"].append({"username": username, "twitch_login": twitch_login})
            else:
                results["not_subs"].append({
                    "username": username,
                    "twitch_login": twitch_login,
                    "telegram_id": tg_id,
                    "twitch_id": twitch_id
                })

        # Generar reporte
        total_ok = len(results["subs_ok"])
        total_bad = len(results["not_subs"])
        total_no_id = len(results["no_twitch_id"])
        total_admins = len(results["admins"])
        total_priv = len(results["privileged"])
        total_ghosts = len(results["not_in_group"])

        report = f"🔍 **VERIFICACIÓN COMPLETA: {slug}**\n{'═'*40}\n\n"
        report += f"📊 **RESUMEN**\n• Total en BD: `{len(members)}`\n• Subs en Twitch: `{len(subs_set)}`\n\n"
        report += f"✅ Subs verificados: `{total_ok}`\n"
        report += f"❌ NO son subs: `{total_bad}`\n"
        report += f"⚠️ Sin twitch_id: `{total_no_id}`\n"
        report += f"👑 Admins: `{total_admins}`\n"
        report += f"🛡️ Privilegiados: `{total_priv}`\n"
        report += f"👻 En BD pero no en grupo: `{total_ghosts}`\n\n{'─'*40}\n\n"

        if results["not_subs"]:
            report += f"🚫 **NO SON SUBS** ({total_bad}):\n_(Serían expulsados)_\n\n"
            for u in results["not_subs"][:20]:
                report += f"• @{u['username']} → TW: `{u['twitch_login']}`\n"
            if total_bad > 20:
                report += f"\n_...y {total_bad - 20} más_\n"
            report += f"\n{'─'*40}\n\n"

        if results["no_twitch_id"]:
            report += f"⚠️ **SIN TWITCH_ID** ({total_no_id}):\n\n"
            for u in results["no_twitch_id"][:15]:
                report += f"• @{u['username']}\n"
            if total_no_id > 15:
                report += f"\n_...y {total_no_id - 15} más_\n"
            report += "\n"

        report += f"{'═'*40}\n💡 **ACCIONES**:\n\n"
        if total_bad > 0 or total_no_id > 0:
            report += f"⚠️ **{total_bad + total_no_id}** usuarios serían expulsados.\n• Usa `/auditnow` cuando estés listo.\n\n"
        else:
            report += "✅ Todos verificados como subs.\n\n"

        if total_ghosts > 0:
            report += f"🧹 **{total_ghosts}** registros huérfanos.\n• Usa `/sync_members` para limpiar.\n"

        await msg.delete()
        await self.send_long_message(context.bot, chat.id, report)

        # CSV si muchos problemas
        if total_bad > 50 or total_no_id > 20:
            csv_content = "username,twitch_login,telegram_id,status\n"
            for u in results["not_subs"]:
                csv_content += f"{u['username']},{u['twitch_login']},{u['telegram_id']},NO_SUB\n"
            for u in results["no_twitch_id"]:
                csv_content += f"{u['username']},N/A,{u['telegram_id']},NO_TWITCH_ID\n"
            
            file = io.BytesIO(csv_content.encode('utf-8'))
            file.name = f"verify_{slug}.csv"
            
            await context.bot.send_document(
                chat_id=chat.id,
                document=file,
                filename=f"verify_{slug}.csv",
                caption="📄 Lista completa de usuarios problemáticos"
            )
