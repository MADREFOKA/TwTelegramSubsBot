# app/auditor.py
import logging
import asyncio
import requests
from datetime import time as dtime
from telegram import Update
from telegram.ext import ContextTypes, JobQueue


class Auditor:
    def __init__(self, db, cfg, send_tg, refresh_twitch_token):
        """
        db: instancia Database
        cfg: configuración global (tokens, URLs)
        send_tg: función async(chat_id, text)
        refresh_twitch_token: función async(broadcaster_row) -> access_token
        """
        self.db = db
        self.cfg = cfg
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token

    # ---------------------------------------------------------------------
    # AUDITORÍA MANUAL — /auditnow
    # ---------------------------------------------------------------------
    async def cmd_auditnow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Auditoría manual, incluye expulsión de usuarios sin vincular."""
        user = update.effective_user
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            return await update.message.reply_text("⚠️ Usa este comando en el grupo premium.")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero para vincular tu canal.")

        if chat.id != b.get("group_id"):
            return await update.message.reply_text("⚠️ Este no es tu grupo premium vinculado.")

        await update.message.reply_text("🔍 Iniciando auditoría manual...")

        stats = await self._run_audit(context, b, chat.id)
        msg = (
            f"✅ Auditoría completada.\n"
            f"👥 Miembros revisados: {stats['checked']}\n"
            f"🚫 Expulsados: {stats['kicked']}\n"
            f"⚙️ Sin vincular: {stats['no_link']}\n"
            f"⚠️ Fallos: {stats['failed']}"
        )
        await update.message.reply_text(msg)

    # ---------------------------------------------------------------------
    # AUDITORÍA AUTOMÁTICA (cada lunes a las 9:00)
    # ---------------------------------------------------------------------
    def schedule_weekly_audit(self, job_queue: JobQueue):
        """Programa la auditoría silenciosa cada lunes a las 9:00 AM (Europe/Madrid)."""
        job_queue.run_daily(
            self._audit_all_streamers,
            time=dtime(hour=9, minute=0),
            days=(0,),  # 0 = Lunes
            name="weekly_audit"
        )
        logging.info("📆 Auditoría semanal programada: lunes 09:00 (Europe/Madrid)")

    async def _audit_all_streamers(self, context: ContextTypes.DEFAULT_TYPE):
        """Ejecuta auditoría para todos los streamers registrados (sin enviar mensajes)."""
        streamers = await self.db.all("SELECT * FROM broadcasters WHERE group_id IS NOT NULL")
        logging.info(f"🧹 Iniciando auditoría automática de {len(streamers)} streamers...")

        for b in streamers:
            try:
                await self._run_audit(context, b, b["group_id"], silent=True)
                await asyncio.sleep(5)
            except Exception as e:
                logging.warning(f"Error auditando {b.get('slug')}: {e}")

        logging.info("✅ Auditoría semanal completada.")

    # ---------------------------------------------------------------------
    # LÓGICA PRINCIPAL DE AUDITORÍA
    # ---------------------------------------------------------------------
    async def _run_audit(self, context, b, group_id, silent=False):
        """Lógica compartida entre /auditnow y auditoría programada."""
        stats = {"checked": 0, "kicked": 0, "failed": 0, "no_link": 0}

        try:
            members = await context.bot.get_chat_administrators(group_id)
            all_members = [m.user for m in members if not m.user.is_bot]

            access_token = await self.refresh_twitch_token(b)
            headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.cfg.client_id}

            whitelist_rows = await self.db.all(
                "SELECT username FROM privileged_users WHERE broadcaster_id=%s",
                b["broadcaster_id"]
            )
            whitelist = [p["username"].lower().lstrip("@") for p in whitelist_rows]

            for member in all_members:
                username = (member.username or "").lower()
                stats["checked"] += 1

                # Saltar propietario o privilegiados
                if username in whitelist or username == (b.get("slug") or "").lower():
                    continue

                row = await self.db.one(
                    "SELECT linked_twitch_id FROM telegram_users WHERE telegram_id=%s",
                    member.id
                )

                # Si no tiene vínculo con Twitch
                if not row or not row.get("linked_twitch_id"):
                    stats["no_link"] += 1
                    try:
                        await context.bot.ban_chat_member(group_id, member.id)
                        stats["kicked"] += 1
                    except Exception as e:
                        logging.warning(f"No se pudo expulsar {username}: {e}")
                        stats["failed"] += 1
                    continue

                twitch_id = row["linked_twitch_id"]

                try:
                    r = requests.get(
                        f"{self.cfg.TW_API}/subscriptions/user",
                        headers=headers,
                        params={"broadcaster_id": b["broadcaster_id"], "user_id": twitch_id},
                        timeout=10
                    )
                    data = r.json().get("data", [])
                    if not data:
                        await context.bot.ban_chat_member(group_id, member.id)
                        stats["kicked"] += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.warning(f"Error verificando sub de {username}: {e}")
                    stats["failed"] += 1

        except Exception as e:
            logging.error(f"Error general auditando {b.get('slug')}: {e}")

        if not silent:
            logging.info(f"Auditoría {b.get('slug')} → {stats}")

        return stats

    # ---------------------------------------------------------------------
    # PRIVILEGIOS (whitelist)
    # ---------------------------------------------------------------------
    async def cmd_priv_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if len(context.args) < 1:
            return await update.message.reply_text("Uso: /priv_add @usuario")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        username = context.args[0].lstrip("@").lower()
        await self.db.exec(
            "INSERT INTO privileged_users(broadcaster_id, username) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            b["broadcaster_id"], username
        )
        await update.message.reply_text(f"✅ @{username} añadido a la whitelist.")

    async def cmd_priv_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if len(context.args) < 1:
            return await update.message.reply_text("Uso: /priv_remove @usuario")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        username = context.args[0].lstrip("@").lower()
        await self.db.exec(
            "DELETE FROM privileged_users WHERE broadcaster_id=%s AND username=%s",
            b["broadcaster_id"], username
        )
        await update.message.reply_text(f"🗑️ @{username} eliminado de la whitelist.")

    async def cmd_priv_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        rows = await self.db.all(
            "SELECT username FROM privileged_users WHERE broadcaster_id=%s",
            b["broadcaster_id"]
        )
        if not rows:
            return await update.message.reply_text("🕸️ No hay usuarios en la whitelist.")

        lista = "\n".join(f"• @{r['username']}" for r in rows)
        await update.message.reply_text(
            f"👑 <b>Usuarios privilegiados:</b>\n{lista}",
            parse_mode="HTML"
        )
