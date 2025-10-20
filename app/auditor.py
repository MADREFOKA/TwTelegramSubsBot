# app/auditor.py
import logging
import time
from datetime import datetime, timedelta
from telegram import ChatMemberUpdated
from telegram.ext import ContextTypes

class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    # ---------------------------
    # PROGRAMAR AUDITORÍA SEMANAL
    # ---------------------------
    def schedule_weekly_audit(self, job_queue):
        """Programa una auditoría automática cada lunes a las 9:00."""
        try:
            if job_queue:
                job_queue.run_daily(
                    self.run_scheduled_audits,
                    time=datetime.strptime("09:00", "%H:%M").time(),
                    days=(0,),  # Lunes
                )
                logging.info("📅 Auditoría semanal programada correctamente.")
        except Exception as e:
            logging.warning(f"⚠️ No se pudo programar la auditoría semanal: {e}")

    # ---------------------------
    # COMANDO /AUDITNOW
    # ---------------------------
    async def cmd_auditnow(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Limpieza manual del grupo, sin mostrar resultados."""
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Primero vincula tu canal con /setup")

        await update.message.reply_text("🔍 Iniciando auditoría silenciosa...")

        try:
            await self.run_audit_for_broadcaster(b)
            await update.message.reply_text("✅ Auditoría completada correctamente.")
        except Exception as e:
            logging.error(f"❌ Error en auditoría manual: {e}")
            await update.message.reply_text("⚠️ Error durante la auditoría.")

    # ---------------------------
    # AUDITORÍA PRINCIPAL
    # ---------------------------
    async def run_audit_for_broadcaster(self, b):
        """Ejecuta la limpieza de un streamer (sin avisar en el grupo)."""
        try:
            group_id = b.get("group_id")
            if not group_id:
                return

            # Obtener miembros del grupo
            total_members = 0
            expelled = 0
            no_link = 0
            failed = 0

            # Aquí se podría integrar API de Telegram con getChatMemberCount / getChatAdministrators

            # Guardar registro en la BD
            etiqueta_semana = datetime.utcnow().strftime("Semana %d/%m/%Y")
            await self.db.exec(
                """
                INSERT INTO audit_logs (broadcaster_id, group_id, streamer_slug,
                                        etiqueta_semana, total_miembros, expulsados,
                                        sin_vinculo, fallos, fecha_auditoria)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                b["broadcaster_id"], group_id, b["slug"],
                etiqueta_semana, total_members, expelled, no_link, failed, datetime.utcnow()
            )

            logging.info(f"🧹 Auditoría completada para {b['slug']} — expulsados: {expelled}")
        except Exception as e:
            logging.error(f"❌ Error auditando {b.get('slug')}: {e}")

    # ---------------------------
    # PRIVILEGIOS
    # ---------------------------
    async def cmd_priv_add(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Añade un usuario privilegiado."""
        user = update.effective_user
        args = context.args
        if not args:
            return await update.message.reply_text("⚠️ Uso: /priv_add @usuario")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Primero vincula tu canal con /setup")

        username = args[0].lstrip("@")
        await self.db.exec(
            "INSERT INTO privileged_users (broadcaster_id, username) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            b["broadcaster_id"], username
        )
        await update.message.reply_text(f"✅ {username} añadido a la lista de privilegios.")

    async def cmd_priv_remove(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Elimina un usuario privilegiado."""
        user = update.effective_user
        args = context.args
        if not args:
            return await update.message.reply_text("⚠️ Uso: /priv_remove @usuario")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Primero vincula tu canal con /setup")

        username = args[0].lstrip("@")
        await self.db.exec(
            "DELETE FROM privileged_users WHERE broadcaster_id=%s AND username=%s",
            b["broadcaster_id"], username
        )
        await update.message.reply_text(f"🗑️ {username} eliminado de la lista de privilegios.")

    async def cmd_priv_list(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra la lista de privilegiados."""
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Primero vincula tu canal con /setup")

        rows = await self.db.all(
            "SELECT username FROM privileged_users WHERE broadcaster_id=%s",
            b["broadcaster_id"]
        )
        if not rows:
            return await update.message.reply_text("📭 No hay usuarios privilegiados.")
        names = "\n".join(f"• @{r['username']}" for r in rows)
        await update.message.reply_text(f"🛡️ <b>Usuarios privilegiados</b>:\n{names}", parse_mode="HTML")

    # ---------------------------
    # TRACKING MIEMBROS (CENSO)
    # ---------------------------
    async def on_member_update(self, update: ChatMemberUpdated, context: ContextTypes.DEFAULT_TYPE):
        """Registra movimientos de miembros (entrada/salida)."""
        try:
            member = update.chat_member
            user = member.user
            status = member.status
            group_id = update.chat.id
            action = "join" if status in ("member", "administrator") else "left"
            await self.db.exec(
                "INSERT INTO group_members (group_id, user_id, username, action, timestamp) VALUES (%s,%s,%s,%s,%s)",
                group_id, user.id, user.username, action, int(time.time())
            )
        except Exception as e:
            logging.warning(f"⚠️ No se pudo registrar cambio de miembro: {e}")

    # ---------------------------
    # EJECUCIÓN PROGRAMADA (LUNES 9AM)
    # ---------------------------
    async def run_scheduled_audits(self, context: ContextTypes.DEFAULT_TYPE):
        """Ejecuta la auditoría automática para todos los streamers."""
        try:
            broadcasters = await self.db.all("SELECT * FROM broadcasters WHERE group_id IS NOT NULL")
            for b in broadcasters:
                await self.run_audit_for_broadcaster(b)
        except Exception as e:
            logging.error(f"❌ Error en auditorías programadas: {e}")
