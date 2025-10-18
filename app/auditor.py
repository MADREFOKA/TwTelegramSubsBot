# app/auditor.py
import logging
import asyncio
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from telegram import Update, ChatMemberUpdated
from telegram.ext import ContextTypes

KICK_NOT_LINKED = False  # ← si quieres que /auditnow expulse "sin vínculo", ponlo en True

class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token
        self.tz = ZoneInfo("Europe/Madrid")

    # ──────────────────────────────────────────────────────────────────────
    # PROGRAMACIÓN SEMANAL (lunes 09:00, Europe/Madrid)
    # ──────────────────────────────────────────────────────────────────────
    def schedule_weekly_audit(self, job_queue):
        if not job_queue:
            logging.warning("⚠️ No hay JobQueue disponible. Saltando auditoría programada.")
            return
        run_time = dtime(hour=9, minute=0, tzinfo=self.tz)
        # Monday = 0
        job_queue.run_daily(self._weekly_task, run_time, days=(0,))
        logging.info("🗓️ Auditoría semanal programada los lunes a las 09:00 Europe/Madrid.")

    async def _weekly_task(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            # Obtener todos los broadcasters con grupo premium vinculado
            broadcasters = await self.db.all("SELECT * FROM broadcasters WHERE group_id IS NOT NULL")
            for b in broadcasters or []:
                try:
                    await self._run_audit_for_group(b, kick=False, silent=True)
                except Exception as e:
                    logging.error(f"❌ Auditoría semanal falló para {b.get('slug')}: {e}")
        except Exception as e:
            logging.error(f"❌ Error en tarea semanal: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # COMANDO MANUAL /auditnow  (ejecutar en el grupo premium)
    # ──────────────────────────────────────────────────────────────────────
    async def cmd_auditnow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            return await update.message.reply_text("⚠️ Ejecuta este comando desde el grupo premium.")

        chat_id = update.effective_chat.id
        user = update.effective_user

        # Verificar que el usuario sea el owner del broadcaster (streamer)
        b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat_id)
        if not b:
            return await update.message.reply_text("❌ Este grupo no está vinculado a ningún streamer con /setgroup.")
        if b.get("owner_telegram_id") != user.id:
            return await update.message.reply_text("🚫 Solo el propietario del canal puede ejecutar /auditnow.")

        # Ejecuta auditoría con expulsión opcional (controlada por KICK_NOT_LINKED)
        kicked = await self._run_audit_for_group(b, kick=KICK_NOT_LINKED, silent=False)
        if kicked is None:
            # Si no hay datos de miembros, informamos de modo suave
            return await update.message.reply_text("ℹ️ Auditoría realizada. Aún no tengo el censo completo de miembros.\n" 
                                                   "El bot actualizará miembros automáticamente cuando entren/salgan.")
        return await update.message.reply_text("✅ Auditoría realizada.")

    # ──────────────────────────────────────────────────────────────────────
    # PRIVILEGIOS (whitelist por username de Telegram)
    # ──────────────────────────────────────────────────────────────────────
    async def cmd_priv_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Primero vincula tu canal con /setup.")
        if not context.args:
            return await update.message.reply_text("Uso: /priv_add @usuario")

        username = context.args[0].lstrip("@").lower()
        await self.db.exec("""
            INSERT INTO privileged_users (broadcaster_id, username)
            VALUES (%s, %s)
            ON CONFLICT (broadcaster_id, username) DO NOTHING
        """, b["broadcaster_id"], username)
        await update.message.reply_text(f"✅ {username} añadido a la whitelist.")

    async def cmd_priv_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Primero vincula tu canal con /setup.")
        if not context.args:
            return await update.message.reply_text("Uso: /priv_remove @usuario")

        username = context.args[0].lstrip("@").lower()
        await self.db.exec("""
            DELETE FROM privileged_users WHERE broadcaster_id=%s AND username=%s
        """, b["broadcaster_id"], username)
        await update.message.reply_text(f"✅ {username} eliminado de la whitelist.")

    async def cmd_priv_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Primero vincula tu canal con /setup.")
        rows = await self.db.all("""
            SELECT username FROM privileged_users WHERE broadcaster_id=%s ORDER BY username
        """, b["broadcaster_id"])
        if not rows:
            return await update.message.reply_text("📝 Whitelist vacía.")
        listado = "\n".join(f"• @{r['username']}" for r in rows)
        await update.message.reply_text(f"🛡️ <b>Whitelist</b>\n{listado}", parse_mode="HTML")

    # ──────────────────────────────────────────────────────────────────────
    # TRACKING DE MIEMBROS (para alimentar group_members)
    # ──────────────────────────────────────────────────────────────────────
    async def on_member_update(self, update: ChatMemberUpdated, context: ContextTypes.DEFAULT_TYPE):
        chat = update.chat
        if chat.type not in ("group", "supergroup"):
            return
        b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat.id)
        if not b:
            return  # no es el grupo premium de un streamer vinculado

        member = update.new_chat_member
        uid = member.user.id
        username = (member.user.username or "").lower()

        if member.status in ("left", "kicked"):
            await self.db.exec("""
                INSERT INTO group_members (group_id, telegram_id, username, is_member, updated_at, streamer_slug)
                VALUES (%s,%s,%s,false,EXTRACT(EPOCH FROM NOW())::BIGINT,%s)
                ON CONFLICT (group_id, telegram_id)
                DO UPDATE SET is_member=false, username=EXCLUDED.username, updated_at=EXCLUDED.updated_at, streamer_slug=EXCLUDED.streamer_slug
            """, chat.id, uid, username, b["slug"])
        else:
            await self.db.exec("""
                INSERT INTO group_members (group_id, telegram_id, username, is_member, updated_at, streamer_slug)
                VALUES (%s,%s,%s,true,EXTRACT(EPOCH FROM NOW())::BIGINT,%s)
                ON CONFLICT (group_id, telegram_id)
                DO UPDATE SET is_member=true, username=EXCLUDED.username, updated_at=EXCLUDED.updated_at, streamer_slug=EXCLUDED.streamer_slug
            """, chat.id, uid, username, b["slug"])

    # ──────────────────────────────────────────────────────────────────────
    # LÓGICA DE AUDITORÍA
    # ──────────────────────────────────────────────────────────────────────
    async def _run_audit_for_group(self, b_row: dict, kick: bool, silent: bool):
        """
        - Cuenta miembros actuales (según tabla group_members).
        - Calcula 'sin vínculo' = miembros del grupo que no han hecho /start (no existen en telegram_users)
          para el streamer en cuestión.
        - (Opcional) Expulsa sin-vínculo si kick=True.
        - Registra en audit_logs con streamer_slug.
        """
        slug = b_row["slug"]
        group_id = b_row["group_id"]
        if not group_id:
            logging.info(f"🔎 {slug}: sin group_id vinculado; auditoría omitida.")
            return None

        # 1) Miembros actuales según nuestro censo
        members = await self.db.all("""
            SELECT telegram_id, username FROM group_members
            WHERE group_id=%s AND is_member=true
        """, group_id)
        total_miembros = len(members or [])

        # Si aún no tenemos censo, deja registro pero informa None al caller
        if total_miembros == 0:
            await self._log_audit(slug, b_row["broadcaster_id"], group_id,
                                  etiqueta=self._week_label(), total=0,
                                  expulsados=0, sin_vinculo=0, fallos=0)
            return None

        # 2) Usuarios sin vínculo (no han usado /start para este streamer)
        #    Definición base: están en el grupo y no aparecen en telegram_users.linked_twitch_id
        #    con relación a este streamer. Como no guardamos mapping a streamer en telegram_users,
        #    aplicamos heurística: "no han pasado por /start" ⇒ no están en telegram_users? No,
        #    telegram_users guarda el último vínculo del usuario con cualquier streamer. Para tu caso:
        #    pedimos que hayan pasado por /start *para este slug*, así que usaremos tabla links
        #    (si la tienes) o caeremos a username vacío => "sin vínculo".
        #
        #    Como alternativa simple: si no existe ningún registro en links(telegram_id, streamer_slug),
        #    los consideramos sin-vínculo.
        linked = await self.db.all("""
            SELECT DISTINCT telegram_id FROM links
            WHERE streamer_slug=%s
        """, slug)
        linked_ids = {r["telegram_id"] for r in (linked or [])}

        sin_vinculo_ids = []
        for m in (members or []):
            if m["telegram_id"] not in linked_ids:
                sin_vinculo_ids.append(m["telegram_id"])

        sin_vinculo = len(sin_vinculo_ids)
        expulsados = 0
        fallos = 0

        # 3) (Opcional) Expulsar sin-vínculo
        if kick and sin_vinculo_ids:
            for uid in sin_vinculo_ids:
                try:
                    await context.bot.ban_chat_member(group_id, uid, until_date=int(datetime.now().timestamp()) + 60)
                    await asyncio.sleep(0.05)
                    try:
                        await context.bot.unban_chat_member(group_id, uid, only_if_banned=True)
                    except Exception:
                        pass
                    expulsados += 1
                except Exception as e:
                    fallos += 1
                    logging.warning(f"⚠️ No se pudo expulsar {uid} en {slug}: {e}")

        # 4) Registrar en audit_logs (usando streamer_slug)
        await self._log_audit(
            slug, b_row["broadcaster_id"], group_id,
            etiqueta=self._week_label(),
            total=total_miembros,
            expulsados=expulsados,
            sin_vinculo=sin_vinculo,
            fallos=fallos
        )

        # 5) Mensajería: semanal silenciosa; /auditnow informativo pero breve (lo maneja caller)
        return expulsados

    async def _log_audit(self, slug: str, broadcaster_id: str, group_id: int,
                         etiqueta: str, total: int, expulsados: int, sin_vinculo: int, fallos: int):
        await self.db.exec("""
            INSERT INTO audit_logs (broadcaster_id, group_id, streamer_slug,
                                    etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        """, broadcaster_id, group_id, slug, etiqueta, total, expulsados, sin_vinculo, fallos)

    def _week_label(self) -> str:
        now = datetime.now(self.tz)
        return now.strftime("%d/%m/%Y")
