# app/auditor.py
import logging
import asyncio
import requests
import time
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
    # REGISTRO DE MIEMBROS EN GRUPO (alta/baja)
    # ---------------------------------------------------------------------
    async def on_member_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Guarda en la base de datos cuando un usuario entra o sale del grupo."""
        try:
            data = update.chat_member
            chat = data.chat
            user = data.new_chat_member.user
            status = data.new_chat_member.status

            b = await self.db.one("SELECT broadcaster_id FROM broadcasters WHERE group_id=%s", chat.id)
            if not b:
                return  # grupo no vinculado, ignorar

            if status in ("member", "administrator"):
                await self.db.exec(
                    "INSERT INTO group_members (group_id, telegram_id, username, joined_at) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (group_id, telegram_id) DO UPDATE SET username=EXCLUDED.username",
                    chat.id, user.id, user.username, int(time.time())
                )
                logging.info(f"👤 {user.username} añadido al registro de {chat.id}")
            elif status in ("left", "kicked"):
                await self.db.exec(
                    "DELETE FROM group_members WHERE group_id=%s AND telegram_id=%s",
                    chat.id, user.id
                )
                logging.info(f"👋 {user.username} eliminado del registro de {chat.id}")

        except Exception as e:
            logging.warning(f"Error procesando cambio de miembro: {e}")

    # ---------------------------------------------------------------------
    # AUDITORÍA MANUAL — /auditnow
    # ---------------------------------------------------------------------
    async def cmd_auditnow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Auditoría manual para limpieza inmediata."""
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
            f"👥 Revisados: {stats['checked']}\n"
            f"🚫 Expulsados: {stats['kicked']}\n"
            f"⚙️ Sin vincular: {stats['no_link']}\n"
            f"⚠️ Fallos: {stats['failed']}"
        )
        await update.message.reply_text(msg)

    # ---------------------------------------------------------------------
    # AUDITORÍA AUTOMÁTICA — Lunes 09:00
    # ---------------------------------------------------------------------
    def schedule_weekly_audit(self, job_queue: JobQueue):
        """Programa la auditoría silenciosa cada lunes a las 9:00 AM (Europe/Madrid)."""
        job_queue.run_daily(
            self._audit_all_streamers,
            time=dtime(hour=9, minute=0),
            days=(0,),  # 0 = lunes
            name="weekly_audit"
        )
        logging.info("📆 Auditoría semanal programada: lunes 09:00 (Europe/Madrid)")

    async def _audit_all_streamers(self, context: ContextTypes.DEFAULT_TYPE):
        """Ejecuta auditoría silenciosa de todos los grupos vinculados."""
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
    # LÓGICA PRINCIPAL DE AUDITORÍA (con batches de 100 usuarios)
    # ---------------------------------------------------------------------
    async def _run_audit(self, context, b, group_id, silent=False):
        stats = {"checked": 0, "kicked": 0, "failed": 0, "no_link": 0}
        try:
            members = await self.db.all(
                "SELECT telegram_id, username FROM group_members WHERE group_id=%s",
                group_id
            )
            if not members:
                logging.info(f"🔸 No hay miembros registrados en grupo {group_id}")
                return stats

            access_token = await self.refresh_twitch_token(b)
            headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.cfg.client_id}

            whitelist_rows = await self.db.all(
                "SELECT username FROM privileged_users WHERE broadcaster_id=%s",
                b["broadcaster_id"]
            )
            whitelist = [p["username"].lower().lstrip("@") for p in whitelist_rows]

            batch_size = 100
            for i in range(0, len(members), batch_size):
                batch = members[i:i+batch_size]
                user_ids = []
                telegram_map = {}

                # Recolectamos usuarios vinculados
                for row in batch:
                    member_id = row["telegram_id"]
                    username = (row["username"] or "").lower()
                    stats["checked"] += 1

                    if username in whitelist:
                        continue

                    link_row = await self.db.one(
                        "SELECT linked_twitch_id FROM telegram_users WHERE telegram_id=%s",
                        member_id
                    )
                    if link_row and link_row["linked_twitch_id"]:
                        twitch_id = link_row["linked_twitch_id"]
                        user_ids.append(twitch_id)
                        telegram_map[twitch_id] = (member_id, username)
                    else:
                        stats["no_link"] += 1
                        try:
                            await context.bot.ban_chat_member(group_id, member_id)
                            stats["kicked"] += 1
                        except Exception:
                            stats["failed"] += 1

                if not user_ids:
                    continue

                # Consulta batch (hasta 100 usuarios)
                params = [("user_id", uid) for uid in user_ids]
                params.append(("broadcaster_id", b["broadcaster_id"]))

                try:
                    r = requests.get(
                        f"{self.cfg.TW_API}/subscriptions",
                        headers=headers,
                        params=params,
                        timeout=15
                    )
                    subs = r.json().get("data", [])
                    subbed_ids = {s["user_id"] for s in subs}

                    for uid in user_ids:
                        member_id, username = telegram_map[uid]
                        if uid not in subbed_ids:
                            await context.bot.ban_chat_member(group_id, member_id)
                            stats["kicked"] += 1
                    await asyncio.sleep(2)
                except Exception as e:
                    logging.warning(f"Error batch Twitch API: {e}")

        except Exception as e:
            logging.error(f"Error general auditando {b.get('slug')}: {e}")

        if not silent:
            logging.info(f"Auditoría {b.get('slug')} → {stats}")
        return stats

    # ---------------------------------------------------------------------
    # PRIVILEGIOS — /priv_add /priv_remove /priv_list
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
