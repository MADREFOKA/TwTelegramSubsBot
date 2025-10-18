# app/auditor.py
import logging
import asyncio
import requests
from telegram import Update
from telegram.ext import ContextTypes

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

    # ---------------------------
    # /AUDITNOW — STREAMER
    # ---------------------------
    async def cmd_auditnow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Revisa todos los miembros del grupo premium y elimina los no-suscriptores."""
        user = update.effective_user
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            return await update.message.reply_text("⚠️ Usa este comando en el grupo premium.")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero para vincular tu canal.")

        # Verificamos que sea el grupo premium
        if chat.id != b.get("group_id"):
            return await update.message.reply_text("⚠️ Este no es tu grupo premium vinculado.")

        await update.message.reply_text("🔍 Iniciando auditoría... esto puede tardar un poco.")

        try:
            members = await context.bot.get_chat_administrators(chat.id)
            all_members = [m.user for m in members if not m.user.is_bot]

            access_token = await self.refresh_twitch_token(b)
            headers = {"Authorization": f"Bearer {access_token}", "Client-Id": self.cfg.client_id}

            # Lista de privilegiados
            priv_rows = await self.db.all("SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"])
            whitelist = [p["username"].lower().lstrip("@") for p in priv_rows]

            kicked = []
            kept = []
            failed = []

            for member in all_members:
                username = (member.username or "").lower()
                if username in whitelist or username == (user.username or "").lower():
                    kept.append(username)
                    continue

                # Buscamos ID de Twitch asociado
                row = await self.db.one("SELECT linked_twitch_id FROM telegram_users WHERE telegram_id=%s", member.id)
                if not row or not row.get("linked_twitch_id"):
                    # No tiene Twitch vinculado → no se puede verificar
                    try:
                        await context.bot.ban_chat_member(chat.id, member.id)
                        kicked.append(username or str(member.id))
                    except Exception:
                        failed.append(username or str(member.id))
                    continue

                twitch_id = row["linked_twitch_id"]

                try:
                    r = requests.get(f"{self.cfg.TW_API}/subscriptions/user",
                                     headers=headers,
                                     params={"broadcaster_id": b["broadcaster_id"], "user_id": twitch_id},
                                     timeout=10)
                    data = r.json().get("data", [])
                    if not data:
                        await context.bot.ban_chat_member(chat.id, member.id)
                        kicked.append(username or str(member.id))
                    else:
                        kept.append(username or str(member.id))
                except Exception as e:
                    logging.warning(f"Error comprobando {username}: {e}")
                    failed.append(username or str(member.id))
                await asyncio.sleep(1.0)  # evita rate limit

            msg = (
                f"✅ Auditoría completada.\n\n"
                f"👑 Mantienen acceso: {len(kept)}\n"
                f"🚫 Expulsados: {len(kicked)}\n"
                f"⚠️ Fallos: {len(failed)}"
            )
            await update.message.reply_text(msg)
            await self.send_tg(user.id, msg)
            logging.info(f"Auditoría {b['slug']}: {msg}")

        except Exception as e:
            logging.exception("Error en auditoría:")
            await update.message.reply_text(f"❌ Error durante la auditoría: {e}")

    # ---------------------------
    # PRIVILEGIOS
    # ---------------------------

    async def cmd_priv_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Añade usuario a la whitelist del streamer."""
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
        """Quita usuario de la whitelist."""
        user = update.effective_user
        if len(context.args) < 1:
            return await update.message.reply_text("Uso: /priv_remove @usuario")

        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        username = context.args[0].lstrip("@").lower()
        await self.db.exec("DELETE FROM privileged_users WHERE broadcaster_id=%s AND username=%s",
                           b["broadcaster_id"], username)
        await update.message.reply_text(f"🗑️ @{username} eliminado de la whitelist.")

    async def cmd_priv_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra la lista de usuarios privilegiados."""
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        rows = await self.db.all("SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"])
        if not rows:
            return await update.message.reply_text("🕸️ No hay usuarios en la whitelist.")

        lista = "\n".join(f"• @{r['username']}" for r in rows)
        await update.message.reply_text(f"👑 <b>Usuarios privilegiados:</b>\n{lista}", parse_mode="HTML")
