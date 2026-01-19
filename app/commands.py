import time
import logging
import secrets
import string
import asyncio
from urllib.parse import quote
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from app.utils.safe_exec import safe_exec
from app.utils.messages import guide_message

class Commands:
    def __init__(self, db, cfg, cipher):
        self.db = db
        self.cfg = cfg
        self.cipher = cipher
        self.TW_API = cfg.TW_API
        self.TW_AUTH = cfg.TW_AUTH
        self.TW_TOKEN = cfg.TW_TOKEN
        self.SCOPE_SETUP = ["channel:read:subscriptions"]
        self.SCOPE_USER = ["user:read:subscriptions", "user:read:email"]
        
    # ───────────────────────────────
    # 🧩 UTILIDAD
    # ───────────────────────────────
    async def send_tg(self, chat_id, text, md=False, html=False):
        import aiohttp
        url = f"https://api.telegram.org/bot{self.cfg.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if md: payload["parse_mode"] = "Markdown"
        elif html: payload["parse_mode"] = "HTML"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"Telegram {resp.status}: {await resp.text()}")
        except Exception as e:
            logging.error(f"send_tg error: {e}")

    # ───────────────────────────────
    # ⚙️ /SETUP — STREAMER
    # ───────────────────────────────
    @safe_exec()
    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
        state = self._gen_state()
        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at) VALUES(%s,%s,%s,%s)",
            state, user.id, "broadcaster_setup", int(time.time())
        )

        redirect_uri = f"{self.cfg.base_url}/twitch/setup/callback"
        scope = quote(" ".join(self.SCOPE_SETUP))
        url = (
            f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code&scope={scope}&state={state}&force_verify=true"
        )

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💜 Vincular canal de Twitch", url=url)]])
        await update.message.reply_text(
            "Pulsa el botón para vincular tu canal de Twitch con el bot:",
            reply_markup=keyboard
        )

    # ───────────────────────────────
    # 🚀 /START — VIEWER
    # ───────────────────────────────
    @safe_exec()
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")
        user = update.effective_user
    
        # Leer argumento /start <slug|id|displayname>
        args = context.args if context.args else []
        if not args:
            return await update.message.reply_text(
                "⚠️ Usa el enlace del streamer. Ejemplo: /start *nickdelstreamer* "
            )
        arg = args[0]

        # Buscar broadcaster por id, slug o display_name
        b = await self.db.one(
            """
            SELECT * FROM broadcasters
            WHERE broadcaster_id=%s OR LOWER(slug)=%s OR LOWER(display_name)=LOWER(%s)
            """,
            arg, arg.lower(), arg
        )
        if not b:
            return await update.message.reply_text("❌ Este streamer no está vinculado.")

        # 🧾 Guardar datos básicos de Telegram en telegram_users incluyendo streamer_id
        try:
            await self.db.exec("""
                INSERT INTO telegram_users (telegram_id, streamer_id, username, first_name, last_name, is_subscribed)
                VALUES (%s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (telegram_id, streamer_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name;
            """, user.id, b["broadcaster_id"], user.username, user.first_name, user.last_name)
            
            logging.info(
                f"💾 Datos Telegram actualizados: {user.first_name} {user.last_name} "
                f"(@{user.username}) streamer_id={b['broadcaster_id']}"
            )
        except Exception as e:
            logging.warning(f"⚠️ No se pudo guardar datos de Telegram: {e}")
    
        # Generar estado OAuth
        state = self._gen_state()
        await self.db.exec(
            "INSERT INTO oauth_states(state,telegram_id,purpose,created_at,broadcaster_id) VALUES(%s,%s,%s,%s,%s)",
            state, user.id, "user_link", int(time.time()), b["broadcaster_id"]
        )
    
        redirect_uri = f"{self.cfg.base_url}/twitch/callback"
        scope = quote(" ".join(self.SCOPE_USER))
        url = (
            f"{self.TW_AUTH}?client_id={self.cfg.client_id}&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code&scope={scope}&state={state}&force_verify=true"
        )
    
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💜 Verificar suscripción", url=url)]])
        await update.message.reply_text(
            f"Pulsa el botón para vincular tu Twitch y comprobar si eres sub de {b['display_name']}:\n\n"
            f"⚠️ Si tienes 2FA o autenticación en Twitch, usa un PC para completar la vinculación.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
    # ───────────────────────────────
    # 🏠 /SETGROUP — GRUPO PREMIUM
    # ───────────────────────────────
    @safe_exec()
    async def cmd_setgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec(
            "UPDATE broadcasters SET group_id=%s, invite_link=%s WHERE broadcaster_id=%s",
            update.effective_chat.id, link.invite_link, b["broadcaster_id"]
        )

        new_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={b['dashboard_token']}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Abrir Dashboard", url=new_link)]])
        try:
            await update.message.delete()
        except:
            pass
        await self.send_tg(
            b["owner_telegram_id"],
            f"✅ Grupo premium vinculado correctamente.\n\n💜 Enlace para viewers:\nhttps://t.me/{self.cfg.bot_username}?start={b['slug']}",
        )

    # ───────────────────────────────
    # 🎁 /SETFREEGROUP — GRUPO GRATUITO
    # ───────────────────────────────
    @safe_exec()
    async def cmd_setfreegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ("group", "supergroup"):
            return
        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ Usa /setup primero.")

        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await self.db.exec(
            "UPDATE broadcasters SET free_group_id=%s, free_invite_link=%s WHERE broadcaster_id=%s",
            update.effective_chat.id, link.invite_link, b["broadcaster_id"]
        )
        try:
            await update.message.delete()
        except:
            pass
        await self.send_tg(b["owner_telegram_id"], f"✅ Grupo gratuito vinculado correctamente.")

    # ───────────────────────────────
    # 🔐 /REGENDASHLINK
    # ───────────────────────────────
    @safe_exec()
    async def cmd_regendashlink(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        user = update.effective_user
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        new_token = secrets.token_urlsafe(32)
        await self.db.exec(
            "UPDATE broadcasters SET dashboard_token=%s WHERE broadcaster_id=%s",
            new_token, b["broadcaster_id"]
        )
        new_link = f"{self.cfg.base_url}/dashboard/{b['slug']}?token={new_token}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Abrir nuevo Dashboard", url=new_link)]])
        await update.message.reply_text(
            "✅ Nuevo enlace del Dashboard generado.\n🛡️ El anterior ha sido invalidado.",
            reply_markup=keyboard
        )

    # ───────────────────────────────
    # 🧹 /LIMPIEZA — LIMPIEZA INICIAL DEL GRUPO PREMIUM
    # ───────────────────────────────
    @safe_exec()
    async def cmd_limpieza(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Limpieza inicial del grupo premium.
        Se ejecuta en privado por el dueño.
        Expulsa del grupo premium a cualquiera que NO esté suscrito
        según telegram_users.is_subscribed para este streamer.
        No toca admins ni privilegiados.
        No genera auditoría ni afecta la gráfica.
        """
        from datetime import datetime, timedelta
        import asyncio

        chat = update.effective_chat
        user = update.effective_user

        # 1. Solo en privado
        if chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado conmigo.")

        # 2. Confirmar que este usuario es dueño de un canal vinculado
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes ningún canal vinculado. Usa /setup primero.")

        group_id = b.get("group_id")
        if not group_id:
            return await update.message.reply_text("❌ Aún no has vinculado tu grupo premium con /setgroup.")

        broadcaster_id = b["broadcaster_id"]

        # 3. Obtener admins actuales del grupo (para no expulsarlos por error)
        try:
            admins = await context.bot.get_chat_administrators(group_id)
            admin_ids = {a.user.id for a in admins}
        except Exception as e:
            admin_ids = set()
            logging.warning(f"⚠️ No se pudieron obtener administradores del grupo {group_id}: {e}")

        # 4. Obtener privilegiados (tabla privileged_users + owner)
        privileged_ids = set(await self.db.get_privileged_ids(b["slug"])) | {b["owner_telegram_id"]}

        # 5. Cargar TODOS los usuarios asociados a este streamer en telegram_users
        rows = await self.db.all("""
            SELECT telegram_id, username, first_name, last_name, is_subscribed
            FROM telegram_users
            WHERE streamer_id=%s
        """, broadcaster_id)

        if not rows:
            return await update.message.reply_text("📭 No hay usuarios registrados aún para este canal.")

        expulsados = 0
        fallos = 0
        saltados_privilegio = 0
        saltados_admin = 0
        ya_sub = 0
        expulsados_total = 0

        # 6. Recorrer cada usuario y expulsar del grupo a los NO suscritos
        for i, r in enumerate(rows, start=1):
            tg_id = r["telegram_id"]
            is_sub = r.get("is_subscribed", False)

            # Saltar admins
            if tg_id in admin_ids:
                saltados_admin += 1
                continue

            # Saltar privilegiados
            if tg_id in privileged_ids:
                saltados_privilegio += 1
                continue

            # Si está marcado como sub -> no tocar
            if is_sub:
                ya_sub += 1
                continue

            # En este punto: no admin, no privilegiado, no sub => expulsar
            try:
                # 🔹 Expulsión temporal (30 segundos)
                until = datetime.now() + timedelta(seconds=30)
                await context.bot.ban_chat_member(group_id, tg_id, until_date=until)
                expulsados += 1
                expulsados_total += 1
                logging.info(f"🧹 Expulsado temporalmente {tg_id} ({r['username']}) por limpieza inicial (no sub).")

                # 🔹 Limpieza también en group_members si estaba ahí
                try:
                    await self.db.exec("""
                        DELETE FROM group_members
                        WHERE group_id=%s AND telegram_id=%s AND broadcaster_id=%s
                    """, group_id, tg_id, broadcaster_id)
                except Exception as e:
                    logging.warning(f"⚠️ No se pudo limpiar group_members para {tg_id}: {e}")

                # 🔹 Pequeño delay para evitar flood (5 expulsiones/seg)
                await asyncio.sleep(0.2)

                # 🔹 Pausa cada 50 expulsiones
                if expulsados_total % 50 == 0:
                    logging.info("⏸️ Pausa de seguridad: 50 expulsiones procesadas, esperando 15s…")
                    await asyncio.sleep(15)

            except Exception as e:
                fallos += 1
                logging.warning(f"⚠️ No se pudo expulsar {tg_id}: {e}")

        # 7. Reporte final para el streamer, en privado
        resumen = (
            "🧹 Limpieza inicial completada.\n\n"
            f"• Expulsados (no sub): {expulsados}\n"
            f"• No expulsados por ser admin: {saltados_admin}\n"
            f"• No expulsados por ser privilegiados: {saltados_privilegio}\n"
            f"• Miembros que ya eran sub: {ya_sub}\n"
            f"• Fallos al expulsar: {fallos}\n\n"
            f"👑 Canal: {b['display_name']}\n"
            f"👥 Grupo: {group_id}\n"
        )

        await update.message.reply_text(resumen)

    # ───────────────────────────────
    # 🧬 AUXILIAR
    # ───────────────────────────────
    def _gen_state(self, n=32):
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

    # ───────────────────────────────
    # 🧬 GUIA DE USO
    # ───────────────────────────────
    async def on_callback(self, update, context):
        """Responde a botones inline con callback_data."""
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
    
        if query.data == "guide_usage":
            await query.message.reply_html(
                guide_message(),
                disable_web_page_preview=True
            )

    # ───────────────────────────────
    # 👑 PRIVILEGIOS — Añadir / Eliminar / Listar
    # ───────────────────────────────
    @safe_exec()
    async def cmd_priv_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Añade un usuario como privilegiado respondiendo a su mensaje."""
        if not update.message or not update.effective_user:
            return
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", update.effective_user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes permiso para usar este comando.")

        if not update.message.reply_to_message:
            return await update.message.reply_text("👉 Usa este comando respondiendo al usuario que quieras añadir.")
        target = update.message.reply_to_message.from_user

        await self.db.add_privileged_user(
            b["slug"], b["broadcaster_id"], target.id, target.username, update.effective_user.id
        )
        await update.message.reply_text(f"✅ {target.username or target.first_name} añadido como usuario privilegiado.")

    @safe_exec()
    async def cmd_priv_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Elimina a un usuario privilegiado."""
        if not update.message or not update.effective_user:
            return
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", update.effective_user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes permiso para usar este comando.")
        if not update.message.reply_to_message:
            return await update.message.reply_text("👉 Usa este comando respondiendo al usuario que quieras eliminar.")
        target = update.message.reply_to_message.from_user

        await self.db.remove_privileged_user(b["slug"], b["broadcaster_id"], target.id)
        await update.message.reply_text(f"🗑️ {target.username or target.first_name} eliminado de privilegiados.")

    @safe_exec()
    async def cmd_priv_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Lista todos los usuarios privilegiados del streamer."""
        if not update.message or not update.effective_user:
            return
        b = await self.db.one("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", update.effective_user.id)
        if not b:
            return await update.message.reply_text("❌ No tienes permiso para usar este comando.")

        rows = await self.db.list_privileged_users(b["slug"])
        if not rows:
            return await update.message.reply_text("📭 No hay usuarios privilegiados actualmente.")
        text = "👑 *Usuarios privilegiados:*\n\n" + "\n".join(
            [f"• {r['username']} (ID: `{r['telegram_id']}`)" for r in rows]
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    # ─────────────────────────────────
    # 👑 Sincronizar miembros del grupo
    # ─────────────────────────────────
    async def cmd_sync_members(self, update, context):
        MI_ID = 5770265726  

        # Seguridad total
        if update.effective_user.id != MI_ID:
            return await update.message.reply_text(
                "❌ No tienes permiso para usar este comando."
            )

        chat = update.effective_chat
        if chat.type != "private":
            return await update.message.reply_text("⚠️ Usa este comando en privado.")

        await update.message.reply_text("🔍 Buscando streamers vinculados…")

        # Leer TODOS los broadcasters que tengan grupo premium
        broadcasters = await self.db.all("""
            SELECT * FROM broadcasters
            WHERE group_id IS NOT NULL
        """)

        if not broadcasters:
            return await update.message.reply_text(
                "❌ Ningún streamer tiene grupo PREMIUM vinculado."
            )

        # Procesar cada streamer
        for b in broadcasters:
            group_id = b["group_id"]

            await update.message.reply_text(
                f"🔄 Sincronizando miembros para <b>{b['slug']}</b>…",
                parse_mode="HTML"
            )

            # 1. Usuarios actuales en group_members
            db_members = await self.db.all("""
                SELECT telegram_id FROM group_members
                WHERE group_id=%s AND broadcaster_id=%s
            """, group_id, b["broadcaster_id"])

            db_ids = {row["telegram_id"] for row in db_members}

            # 2. Usuarios del streamer en telegram_users
            all_users = await self.db.all("""
                SELECT telegram_id FROM telegram_users
                WHERE streamer_id=%s
            """, b["broadcaster_id"])

            still_inside = set()
            removed = set()

            for row in all_users:
                tg_id = row["telegram_id"]

                try:
                    member = await context.bot.get_chat_member(group_id, tg_id)
                    if member.status in (
                        "member", "administrator", "creator", "restricted"
                    ):
                        still_inside.add(tg_id)
                    else:
                        removed.add(tg_id)

                except Exception:
                    removed.add(tg_id)

                await asyncio.sleep(0.05)

            # 3. Añadir los que están dentro pero no en DB
            added_count = 0
            for tg_id in still_inside:
                if tg_id not in db_ids:
                    await self.db.exec("""
                        INSERT INTO group_members (
                            group_id, telegram_id, username,
                            broadcaster_id, streamer_slug, is_subscribed, joined_at
                        )
                        VALUES (%s,%s,NULL,%s,%s,TRUE,NOW())
                        ON CONFLICT DO NOTHING
                    """, group_id, tg_id,
                         b["broadcaster_id"], b["slug"])
                    added_count += 1

            # 4. Borrar los que están en DB pero NO dentro del grupo real
            removed_count = 0
            for tg_id in db_ids:
                if tg_id not in still_inside:
                    await self.db.exec("""
                        DELETE FROM group_members
                        WHERE group_id=%s AND telegram_id=%s
                    """, group_id, tg_id)
                    removed_count += 1

            await update.message.reply_text(
                f"🏁 <b>{b['slug']} sincronizado</b>:\n"
                f"➕ Añadidos: <b>{added_count}</b>\n"
                f"➖ Eliminados: <b>{removed_count}</b>\n",
                parse_mode="HTML"
            )

        await update.message.reply_text("✅ Sincronización completa para todos los streamers.")
# commands.py - Añadir este método a la clase Commands

async def cmd_sync_group(self, update, context):
    """
    🔄 SINCRONIZACIÓN COMPLETA DEL GRUPO
    
    Compara los miembros REALES del grupo de Telegram con group_members en BD.
    
    Proceso:
    1. Obtiene TODOS los miembros del grupo (usando getChatMember iterativo)
    2. Compara con group_members
    3. Añade los que faltan (marcados como NO verificados)
    4. Genera reporte detallado
    
    ⚠️ NO expulsa a nadie, solo añade registros faltantes
    """
    
    user = update.effective_user
    chat = update.effective_chat
    
    # ═══════════════════════════════════════════════════════════
    # PERMISOS: Solo superadmin
    # ═══════════════════════════════════════════════════════════
    
    if user.id != self.cfg.bot_owner_id:
        return await update.message.reply_text(
            "❌ Solo el superadmin puede usar este comando."
        )
    
    # Permitir uso en privado con slug, o en el grupo directamente
    if chat.type == "private":
        if not context.args:
            return await update.message.reply_text(
                "Usa: /sync_group <slug>\n"
                "O ejecuta el comando dentro del grupo que quieres sincronizar."
            )
        slug = context.args[0].lower()
        b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
        if not b:
            return await update.message.reply_text("❌ Slug no encontrado.")
        if not b.get("group_id"):
            return await update.message.reply_text("❌ Este streamer no tiene grupo premium.")
        group_id = b["group_id"]
    
    elif chat.type in ("group", "supergroup"):
        # Ejecutado dentro del grupo
        b = await self.db.one("SELECT * FROM broadcasters WHERE group_id=%s", chat.id)
        if not b:
            return await update.message.reply_text("❌ Este grupo no está vinculado.")
        slug = b["slug"]
        group_id = chat.id
    
    else:
        return await update.message.reply_text("⚠️ Usa este comando en privado o dentro del grupo.")
    
    # ═══════════════════════════════════════════════════════════
    # INICIAR PROCESO
    # ═══════════════════════════════════════════════════════════
    
    msg = await update.message.reply_text(
        f"🔄 Sincronizando grupo de **{slug}**...\n"
        f"⏳ Esto puede tardar varios minutos si el grupo es grande.\n\n"
        f"📊 Fase 1/3: Obteniendo miembros del grupo...",
        parse_mode="Markdown"
    )
    
    broadcaster_id = b["broadcaster_id"]
    
    # ═══════════════════════════════════════════════════════════
    # FASE 1: Obtener TODOS los miembros del grupo de Telegram
    # ═══════════════════════════════════════════════════════════
    
    telegram_members = {}  # {telegram_id: {username, status}}
    
    try:
        # Primero obtener admins (estos siempre son accesibles)
        admins = await context.bot.get_chat_administrators(group_id)
        admin_ids = set()
        
        for admin in admins:
            telegram_members[admin.user.id] = {
                "username": admin.user.username,
                "first_name": admin.user.first_name,
                "status": admin.status,
                "is_admin": True
            }
            admin_ids.add(admin.user.id)
        
        logging.info(f"✅ {len(admins)} administradores encontrados")
        
        # Ahora obtener el conteo total de miembros
        try:
            chat_info = await context.bot.get_chat(group_id)
            total_members = getattr(chat_info, 'member_count', 0)
            logging.info(f"📊 Total de miembros reportado por Telegram: {total_members}")
        except Exception as e:
            total_members = 0
            logging.warning(f"⚠️ No se pudo obtener member_count: {e}")
        
        await msg.edit_text(
            f"🔄 Sincronizando grupo de **{slug}**...\n"
            f"⏳ Fase 1/3: Escaneando miembros...\n\n"
            f"👑 Admins encontrados: {len(admins)}\n"
            f"📊 Total estimado: {total_members}\n\n"
            f"_Nota: Telegram limita la lectura masiva de miembros._\n"
            f"_Usaremos los datos de group_members existentes como base._",
            parse_mode="Markdown"
        )
        
        # ═══════════════════════════════════════════════════════════
        # ESTRATEGIA ALTERNATIVA:
        # Como Telegram no permite listar todos los miembros fácilmente,
        # vamos a verificar los que YA están en group_members + detectar nuevos
        # ═══════════════════════════════════════════════════════════
        
        # Obtener usuarios actuales en BD
        db_members = await self.db.all("""
            SELECT telegram_id, username, twitch_id, twitch_login, is_subscribed
            FROM group_members
            WHERE group_id=%s AND broadcaster_id=%s
        """, group_id, broadcaster_id)
        
        db_ids = {m["telegram_id"] for m in db_members}
        
        logging.info(f"📊 Miembros en group_members: {len(db_ids)}")
        
        # Verificar cada uno si sigue en el grupo
        still_in_group = set()
        left_group = set()
        
        for i, m in enumerate(db_members):
            tg_id = m["telegram_id"]
            
            # Actualizar progreso cada 50
            if i > 0 and i % 50 == 0:
                await msg.edit_text(
                    f"🔄 Verificando miembros existentes...\n"
                    f"📊 Procesados: {i}/{len(db_members)}",
                    parse_mode="Markdown"
                )
                await asyncio.sleep(0.5)  # Evitar rate limit
            
            try:
                member = await context.bot.get_chat_member(group_id, tg_id)
                if member.status in ("member", "administrator", "creator", "restricted"):
                    still_in_group.add(tg_id)
                    if tg_id not in telegram_members:
                        telegram_members[tg_id] = {
                            "username": member.user.username,
                            "first_name": member.user.first_name,
                            "status": member.status,
                            "is_admin": member.status in ("administrator", "creator")
                        }
                else:
                    left_group.add(tg_id)
            except Exception as e:
                # Usuario probablemente ya no está
                left_group.add(tg_id)
            
            await asyncio.sleep(0.05)  # Rate limit
        
    except Exception as e:
        logging.exception("Error obteniendo miembros del grupo")
        return await msg.edit_text(
            f"❌ Error al acceder al grupo:\n`{str(e)}`\n\n"
            f"Asegúrate de que el bot es administrador del grupo.",
            parse_mode="Markdown"
        )
    
    # ═══════════════════════════════════════════════════════════
    # FASE 2: Detectar diferencias
    # ═══════════════════════════════════════════════════════════
    
    await msg.edit_text(
        f"🔄 Fase 2/3: Analizando diferencias...\n\n"
        f"📊 En grupo real: ~{total_members}\n"
        f"📊 En BD actualmente: {len(db_ids)}\n"
        f"✅ Verificados en grupo: {len(still_in_group)}\n"
        f"🚪 Salieron del grupo: {len(left_group)}",
        parse_mode="Markdown"
    )
    
    # Usuarios que están en el grupo pero NO en BD
    # (Esto solo detecta admins y los ya verificados)
    missing_in_db = set(telegram_members.keys()) - db_ids
    
    # ═══════════════════════════════════════════════════════════
    # FASE 3: Actualizar BD
    # ═══════════════════════════════════════════════════════════
    
    added = 0
    removed = 0
    errors = 0
    
    # 1. Añadir los que faltan (marcados como NO verificados)
    for tg_id in missing_in_db:
        try:
            member_info = telegram_members[tg_id]
            
            await self.db.exec("""
                INSERT INTO group_members (
                    group_id, telegram_id, username, 
                    broadcaster_id, streamer_slug, 
                    is_subscribed, joined_at
                )
                VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
                ON CONFLICT (group_id, telegram_id) DO NOTHING
            """, group_id, tg_id, member_info["username"], 
                 broadcaster_id, slug)
            
            added += 1
            logging.info(f"➕ Añadido: @{member_info['username']} ({tg_id})")
            
        except Exception as e:
            errors += 1
            logging.error(f"❌ Error añadiendo {tg_id}: {e}")
    
    # 2. Eliminar los que ya no están
    for tg_id in left_group:
        try:
            await self.db.exec("""
                DELETE FROM group_members
                WHERE group_id=%s AND telegram_id=%s
            """, group_id, tg_id)
            
            removed += 1
            logging.info(f"🗑️ Eliminado: {tg_id} (ya no está en el grupo)")
            
        except Exception as e:
            errors += 1
            logging.error(f"❌ Error eliminando {tg_id}: {e}")
    
    # ═══════════════════════════════════════════════════════════
    # REPORTE FINAL
    # ═══════════════════════════════════════════════════════════
    
    await msg.edit_text(
        f"✅ **Sincronización completada para {slug}**\n"
        f"{'═'*40}\n\n"
        f"📊 **RESUMEN**:\n"
        f"• Total en grupo (Telegram): ~{total_members}\n"
        f"• Verificados activos: {len(still_in_group)}\n"
        f"• En BD antes: {len(db_ids)}\n"
        f"• En BD después: {len(db_ids) + added - removed}\n\n"
        f"{'─'*40}\n\n"
        f"📥 **CAMBIOS**:\n"
        f"• ➕ Añadidos: **{added}**\n"
        f"• 🗑️ Eliminados: **{removed}**\n"
        f"• ❌ Errores: **{errors}**\n\n"
        f"{'─'*40}\n\n"
        f"⚠️ **IMPORTANTE**:\n"
        f"Los {added} usuarios añadidos están marcados como:\n"
        f"• `is_subscribed = FALSE`\n"
        f"• `twitch_id = NULL`\n\n"
        f"Necesitan verificarse con `/start {slug}` para completar sus datos.\n\n"
        f"💡 **Siguiente paso**:\n"
        f"Ejecuta `/verify` para ver quiénes necesitan verificarse.",
        parse_mode="Markdown"
    )
    
    # ═══════════════════════════════════════════════════════════
    # REPORTE DETALLADO ADICIONAL
    # ═══════════════════════════════════════════════════════════
    
    # Generar lista de los añadidos (si no son muchos)
    if added > 0 and added <= 50:
        added_list = "\n\n📝 **Usuarios añadidos**:\n"
        for tg_id in missing_in_db:
            member_info = telegram_members.get(tg_id, {})
            username = member_info.get("username", f"ID:{tg_id}")
            is_admin = " 👑" if member_info.get("is_admin") else ""
            added_list += f"• @{username}{is_admin}\n"
        
        if len(added_list) < 3000:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=added_list,
                parse_mode="Markdown"
            )
    
    elif added > 50:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"ℹ️ Se añadieron {added} usuarios (demasiados para listar).\n"
                 f"Ejecuta `/verify` para ver el estado completo.",
            parse_mode="Markdown"
        )
