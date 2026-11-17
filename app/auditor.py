# app/auditor.py
import logging
import os
import requests
import asyncio
import io
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from app.db import Database

# =====================================================================
#                    CONFIGURACIÓN DE SEGURIDAD
# =====================================================================

# Horas que un miembro tiene que aparecer como "no sub" de forma continuada
# antes de expulsarlo (cubre renovaciones Prime, regalos que caducan y
# cobros que tardan unas horas en procesarse).
GRACE_HOURS = float(os.getenv("AUDIT_GRACE_HOURS", "48"))

# Si una auditoría va a expulsar a más de este % de miembros "normales",
# se aborta: casi siempre significa que la lista de Twitch vino mal.
MAX_KICK_RATIO = float(os.getenv("AUDIT_MAX_KICK_RATIO", "0.30"))

# Tolerancia entre el "total" que reporta Twitch y los IDs recibidos.
MIN_LIST_COMPLETENESS = float(os.getenv("AUDIT_MIN_COMPLETENESS", "0.95"))

TW_SUBS_URL = "https://api.twitch.tv/helix/subscriptions"

# Evita dos auditorías simultáneas del mismo streamer
_audit_locks: dict[str, asyncio.Lock] = {}


# =====================================================================
#                    HELPERS TWITCH
# =====================================================================

def _fetch_subs_blocking(broadcaster_id, headers):
    """
    Devuelve (subs_set, total_reportado, err)
      - err: (status_code, resp_text) si falla cualquier página
    """
    subs = set()
    total = None
    cursor = None
    pages = 0

    while True:
        params = {"broadcaster_id": broadcaster_id, "first": 100}
        if cursor:
            params["after"] = cursor

        resp = requests.get(TW_SUBS_URL, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            return None, None, (resp.status_code, resp.text)

        payload = resp.json()
        if total is None:
            total = payload.get("total")
        for s in payload.get("data", []):
            subs.add(str(s["user_id"]))

        pages += 1
        new_cursor = payload.get("pagination", {}).get("cursor")
        # Protección contra cursores que se repiten (bucle infinito)
        if not new_cursor or new_cursor == cursor or pages > 1000:
            break
        cursor = new_cursor

    return subs, total, None


def fetch_all_subs(broadcaster_id, headers):
    """Compat con el código existente (oauth.py, /verify, /auditcheck)."""
    subs, _total, err = _fetch_subs_blocking(broadcaster_id, headers)
    return subs, err


def _confirm_subs_blocking(broadcaster_id, headers, user_ids):
    """
    Segunda comprobación INDIVIDUAL (en lotes de 100) para los que no
    aparecieron en la lista paginada. Devuelve (set_confirmados_sub, err).
    """
    confirmed = set()
    ids = [str(u) for u in user_ids]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        params = [("broadcaster_id", broadcaster_id)] + [("user_id", u) for u in chunk]
        resp = requests.get(TW_SUBS_URL, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            return None, (resp.status_code, resp.text)
        for s in resp.json().get("data", []):
            confirmed.add(str(s["user_id"]))
    return confirmed, None


async def _twitch_call(fn, b, cfg, refresh_twitch_token, *args):
    """Ejecuta una llamada a Twitch fuera del event loop y reintenta con
    refresh FORZADO si devuelve 401."""
    token = await refresh_twitch_token(b)
    headers = {"Authorization": f"Bearer {token}", "Client-Id": cfg.client_id}
    result = await asyncio.to_thread(fn, b["broadcaster_id"], headers, *args)
    err = result[-1]
    if err and err[0] == 401:
        logging.warning("⚠️ Twitch 401: forzando refresh del token…")
        # token_obtained_at=0 obliga a refresh_twitch_token a renovar de verdad
        token = await refresh_twitch_token({**b, "token_obtained_at": 0})
        headers["Authorization"] = f"Bearer {token}"
        result = await asyncio.to_thread(fn, b["broadcaster_id"], headers, *args)
    return result


async def _kick(bot, group_id, tg_id):
    """Expulsa sin dejar baneado: ban + unban(only_if_banned)."""
    await bot.ban_chat_member(group_id, tg_id)
    await asyncio.sleep(0.3)
    for _ in range(3):
        try:
            await bot.unban_chat_member(group_id, tg_id, only_if_banned=True)
            return
        except Exception as e:
            logging.warning(f"⚠️ Reintentando unban de {tg_id}: {e}")
            await asyncio.sleep(1)
    logging.error(f"❌ {tg_id} se ha quedado BANEADO (unban falló 3 veces)")


# =====================================================================
#                           AUDITORÍA COMPLETA
# =====================================================================

async def run_full_audit(bot, db: Database, cfg, slug: str, refresh_twitch_token):
    lock = _audit_locks.setdefault(slug, asyncio.Lock())
    if lock.locked():
        logging.warning(f"⏭️ Ya hay una auditoría en curso para {slug}, se ignora esta.")
        return
    async with lock:
        await _run_full_audit(bot, db, cfg, slug, refresh_twitch_token)


async def _run_full_audit(bot, db: Database, cfg, slug: str, refresh_twitch_token):
    """
    - Modo seguro: cualquier duda sobre la lista de Twitch → NO se expulsa a nadie
    - twitch_id: si falta en group_members se recupera de telegram_users
    - Doble verificación individual antes de expulsar
    - Periodo de gracia (GRACE_HOURS) antes de expulsar
    - Límite de expulsiones por auditoría (MAX_KICK_RATIO)
    """
    audit_id = None
    total = expulsados = fallos = 0

    async def log_detail(m, accion, motivo, twitch_id=None):
        await db.exec("""
            INSERT INTO audit_details
            (audit_id, telegram_id, username, twitch_login, twitch_id, accion, motivo)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, audit_id, m and m["telegram_id"], m and m.get("username"),
           m and m.get("twitch_login"), twitch_id, accion, motivo)

    async def abort(motivo):
        nonlocal fallos
        fallos += 1
        logging.error(f"⛔ AUDITORÍA ABORTADA ({slug}): {motivo}")
        await log_detail(None, "fallo", f"Auditoría cancelada: {motivo}")

    try:
        b = await db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b or not b.get("group_id"):
            logging.warning(f"⚠️ Saltando auditoría: {slug} sin grupo vinculado.")
            return

        group_id = b["group_id"]
        broadcaster_id = b["broadcaster_id"]
        logging.info(f"🔎 Iniciando auditoría para {slug} (group_id={group_id})")

        await db.exec("""
            CREATE TABLE IF NOT EXISTS audit_strikes (
                group_id    BIGINT NOT NULL,
                telegram_id BIGINT NOT NULL,
                first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (group_id, telegram_id)
            )
        """)

        # twitch_id: group_members y, si falta, el vinculado en telegram_users
        members = await db.all("""
            SELECT gm.telegram_id, gm.username, gm.twitch_login,
                   COALESCE(gm.twitch_id, tu.linked_twitch_id) AS twitch_id,
                   s.first_seen AS not_sub_since
            FROM group_members gm
            LEFT JOIN telegram_users tu
                   ON tu.telegram_id = gm.telegram_id AND tu.streamer_id = gm.broadcaster_id
            LEFT JOIN audit_strikes s
                   ON s.group_id = gm.group_id AND s.telegram_id = gm.telegram_id
            WHERE gm.group_id=%s AND gm.broadcaster_id=%s
        """, group_id, broadcaster_id)
        total = len(members)

        row = await db.one("""
            INSERT INTO audit_logs (broadcaster_id, group_id, streamer_slug)
            VALUES (%s,%s,%s) RETURNING id
        """, broadcaster_id, group_id, slug)
        audit_id = row["id"]

        # Protegidos: admins + privilegiados + dueño del canal
        admins = await bot.get_chat_administrators(group_id)
        protected = {a.user.id for a in admins}
        for x in (await db.get_privileged_ids(slug)) or []:
            try:
                protected.add(int(x))
            except Exception:
                pass
        if b.get("owner_telegram_id"):
            protected.add(int(b["owner_telegram_id"]))

        # ─── 1. Lista paginada de Twitch ───
        subs_set, tw_total, err = await _twitch_call(
            _fetch_subs_blocking, b, cfg, refresh_twitch_token)
        if err:
            return await abort(f"Twitch {err[0]} al obtener la lista de subs")
        if not subs_set:
            return await abort("Twitch devolvió una lista de subs VACÍA")
        if tw_total and len(subs_set) < tw_total * MIN_LIST_COMPLETENESS:
            return await abort(
                f"lista incompleta: {len(subs_set)} IDs recibidos de {tw_total} que reporta Twitch")
        logging.info(f"✅ Lista de subs: {len(subs_set)} (Twitch total={tw_total})")

        # ─── 2. Clasificar ───
        candidates = []      # no aparecen en la lista
        sin_twitch = []      # sin twitch_id ni en group_members ni en telegram_users
        normales = 0
        for m in members:
            tg_id = int(m["telegram_id"])
            if tg_id in protected:
                continue
            normales += 1
            if not m.get("twitch_id"):
                sin_twitch.append(m)
            elif str(m["twitch_id"]) not in subs_set:
                candidates.append(m)

        # ─── 3. Doble verificación individual ───
        if candidates:
            confirmed, err = await _twitch_call(
                _confirm_subs_blocking, b, cfg, refresh_twitch_token,
                [m["twitch_id"] for m in candidates])
            if err:
                return await abort(f"Twitch {err[0]} en la verificación individual")
            if confirmed:
                logging.warning(
                    f"⚠️ {len(confirmed)} usuarios NO estaban en la lista paginada pero SÍ son subs "
                    f"(se habrían expulsado por error)")
            candidates = [m for m in candidates if str(m["twitch_id"]) not in confirmed]

        no_subs_ids = {int(m["telegram_id"]) for m in candidates}

        # ─── 4. Freno de emergencia ───
        if normales and len(candidates) / normales > MAX_KICK_RATIO:
            return await abort(
                f"se iba a expulsar a {len(candidates)}/{normales} miembros "
                f"(> {int(MAX_KICK_RATIO * 100)}%). Revisa con /verify")

        # ─── 5. Actualizar estado de los que SÍ son sub ───
        for m in members:
            tg_id = int(m["telegram_id"])
            if not m.get("twitch_id") or tg_id in no_subs_ids:
                continue
            await db.exec("UPDATE group_members SET is_subscribed=TRUE, twitch_id=%s "
                          "WHERE telegram_id=%s AND group_id=%s", str(m["twitch_id"]), tg_id, group_id)
            await db.exec("UPDATE telegram_users SET is_subscribed=TRUE "
                          "WHERE telegram_id=%s AND streamer_id=%s", tg_id, broadcaster_id)
            if m.get("not_sub_since"):
                await db.exec("DELETE FROM audit_strikes WHERE group_id=%s AND telegram_id=%s",
                              group_id, tg_id)

        # Los sin twitch_id NO se expulsan automáticamente: se registran para revisión
        for m in sin_twitch:
            await log_detail(m, "aviso", "sin twitch_id vinculado (no se expulsa; que haga /start)")

        # ─── 6. Expulsar sólo a los que superan el periodo de gracia ───
        now = datetime.now(timezone.utc)
        en_gracia = 0
        for m in candidates:
            tg_id = int(m["telegram_id"])
            since = m.get("not_sub_since")
            if since is None:
                await db.exec("""
                    INSERT INTO audit_strikes (group_id, telegram_id) VALUES (%s,%s)
                    ON CONFLICT DO NOTHING
                """, group_id, tg_id)
                since = now
            if now - since < timedelta(hours=GRACE_HOURS):
                en_gracia += 1
                await log_detail(m, "aviso", f"no sub desde {since:%d/%m %H:%M} UTC (en gracia)",
                                 m["twitch_id"])
                continue

            try:
                await _kick(bot, group_id, tg_id)
            except Exception as e:
                fallos += 1
                logging.error(f"❌ Error expulsando {tg_id}: {e}")
                await log_detail(m, "fallo", f"Error al expulsar: {e}", m["twitch_id"])
                continue

            await log_detail(m, "expulsado", "ya no es sub (confirmado 2 veces + gracia)",
                             m["twitch_id"])
            await db.exec("UPDATE telegram_users SET is_subscribed=FALSE "
                          "WHERE telegram_id=%s AND streamer_id=%s", tg_id, broadcaster_id)
            await db.exec("DELETE FROM group_members WHERE group_id=%s AND telegram_id=%s",
                          group_id, tg_id)
            await db.exec("DELETE FROM audit_strikes WHERE group_id=%s AND telegram_id=%s",
                          group_id, tg_id)
            expulsados += 1
            logging.info(f"🚫 Expulsado (no sub): {m.get('username') or tg_id}")

            await asyncio.sleep(0.2)
            if expulsados % 50 == 0:
                await asyncio.sleep(15)

        logging.info(
            f"📊 Auditoría {slug}: {total} miembros, {expulsados} expulsados, "
            f"{en_gracia} en gracia, {len(sin_twitch)} sin twitch_id, {fallos} fallos")

    except Exception as e:
        fallos += 1
        logging.exception(f"❌ Error en run_full_audit({slug}): {e}")

    finally:
        if audit_id is not None:
            await db.exec("""
                UPDATE audit_logs
                SET total_miembros=%s, expulsados=%s, fallos=%s, fecha_auditoria=NOW()
                WHERE id=%s
            """, total, expulsados, fallos, audit_id)


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

        # Obtener lista de Twitch (con total reportado)
        try:
            subs_set, tw_total, err = await _twitch_call(
                _fetch_subs_blocking, b, self.cfg, self.refresh_twitch_token)
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
            SELECT gm.telegram_id, gm.username, gm.twitch_login, gm.is_subscribed,
                   COALESCE(gm.twitch_id, tu.linked_twitch_id) AS twitch_id
            FROM group_members gm
            LEFT JOIN telegram_users tu
                   ON tu.telegram_id = gm.telegram_id AND tu.streamer_id = gm.broadcaster_id
            WHERE gm.group_id=%s AND gm.broadcaster_id=%s
            ORDER BY gm.username
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

        # Doble verificación individual de los "no subs"
        falsos_negativos = 0
        confirm_err = None
        if results["not_subs"]:
            confirmed, confirm_err = await _twitch_call(
                _confirm_subs_blocking, b, self.cfg, self.refresh_twitch_token,
                [u["twitch_id"] for u in results["not_subs"]])
            if not confirm_err and confirmed:
                still_bad = []
                for u in results["not_subs"]:
                    if str(u["twitch_id"]) in confirmed:
                        results["subs_ok"].append(u)
                        falsos_negativos += 1
                    else:
                        still_bad.append(u)
                results["not_subs"] = still_bad

        # Generar reporte
        total_ok = len(results["subs_ok"])
        total_bad = len(results["not_subs"])
        total_no_id = len(results["no_twitch_id"])
        total_admins = len(results["admins"])
        total_priv = len(results["privileged"])
        total_ghosts = len(results["not_in_group"])

        report = f"🔍 **VERIFICACIÓN COMPLETA: {slug}**\n{'═'*40}\n\n"
        report += (f"📊 **RESUMEN**\n• Total en BD: `{len(members)}`\n"
                   f"• Subs en Twitch (lista): `{len(subs_set)}` de `{tw_total}` que reporta Twitch\n")
        if tw_total and len(subs_set) < tw_total * MIN_LIST_COMPLETENESS:
            report += "⚠️ La lista de Twitch llegó INCOMPLETA (la auditoría se cancelaría)\n"
        if falsos_negativos:
            report += (f"🔁 `{falsos_negativos}` faltaban en la lista pero SON subs "
                       f"(antes se habrían expulsado)\n")
        if confirm_err:
            report += f"⚠️ No se pudo hacer la verificación individual (Twitch {confirm_err[0]})\n"
        report += "\n"
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
        normales = total_ok + total_bad + total_no_id
        if normales and total_bad / normales > MAX_KICK_RATIO:
            report += (f"⛔ **{total_bad}/{normales}** no son subs (> {int(MAX_KICK_RATIO*100)}%): "
                       f"la auditoría se CANCELARÍA por seguridad.\n\n")
        elif total_bad > 0:
            report += (f"⚠️ **{total_bad}** usuarios serían expulsados tras {GRACE_HOURS:g}h de gracia.\n"
                       f"• Usa `/auditnow` cuando estés listo.\n\n")
        if total_no_id > 0:
            report += f"ℹ️ **{total_no_id}** sin twitch_id: NO se expulsan, deben hacer /start.\n\n"
        if total_bad == 0 and total_no_id == 0:
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
