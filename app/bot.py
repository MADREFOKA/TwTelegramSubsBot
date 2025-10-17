"""
Telegram–Twitch Sub Bot (bot_twitch_linker.py)
"""

import asyncio, logging, os, secrets, string, time, psycopg, requests, base64, hashlib
from psycopg.rows import dict_row
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime, timezone
from dotenv import load_dotenv

# ---------------------------
# CONFIG
# ---------------------------
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET")
BASE_URL = os.getenv("BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME") or "TwSubCheck_bot"
OAUTH_CLIENT_ID_VIEWER = os.getenv("OAUTH_CLIENT_ID_VIEWER") or OAUTH_CLIENT_ID

logging.basicConfig(level=logging.ERROR, format="%(asctime)s | %(levelname)s | %(message)s")

TW_OAUTH_AUTH = "https://id.twitch.tv/oauth2/authorize"
TW_OAUTH_TOKEN = "https://id.twitch.tv/oauth2/token"
TW_API = "https://api.twitch.tv/helix"

SCOPE_SETUP = ["channel:read:subscriptions"]
SCOPE_USER = ["user:read:email"]

print("🧩 DEBUG CLIENTS:", OAUTH_CLIENT_ID_VIEWER, OAUTH_CLIENT_ID)
# ---------------------------
# DB INIT
# ---------------------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS telegram_users (
  telegram_id BIGINT PRIMARY KEY,
  username TEXT,
  linked_twitch_id TEXT
);
CREATE TABLE IF NOT EXISTS twitch_users (
  twitch_id TEXT PRIMARY KEY,
  login TEXT,
  display_name TEXT,
  email TEXT
);
CREATE TABLE IF NOT EXISTS broadcasters (
  broadcaster_id TEXT PRIMARY KEY,
  owner_telegram_id BIGINT NOT NULL,
  access_token TEXT NOT NULL,
  refresh_token TEXT,
  token_obtained_at INTEGER NOT NULL,
  token_expires_in INTEGER NOT NULL,
  group_id BIGINT,
  invite_link TEXT,
  free_group_id BIGINT,
  free_invite_link TEXT,
  slug TEXT
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  telegram_id BIGINT NOT NULL,
  purpose TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  broadcaster_id TEXT
);
CREATE TABLE IF NOT EXISTS oauth_pkce (
  state TEXT PRIMARY KEY,
  code_verifier TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS privileged_users (
  broadcaster_id TEXT,
  username TEXT,
  PRIMARY KEY (broadcaster_id, username)
);
CREATE INDEX IF NOT EXISTS idx_broadcaster_slug ON broadcasters(slug);
CREATE INDEX IF NOT EXISTS idx_oauth_states_telegram ON oauth_states(telegram_id);
CREATE INDEX IF NOT EXISTS idx_tg_users_twitchid ON telegram_users(linked_twitch_id);

ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS last_check INTEGER;
ALTER TABLE broadcasters ADD COLUMN IF NOT EXISTS free_group_id BIGINT;
ALTER TABLE broadcasters ADD COLUMN IF NOT EXISTS free_invite_link TEXT;
"""

async def db_init():
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as con:
        async with con.cursor() as cur:
            for stmt in [x.strip() for x in CREATE_SQL.split(";") if x.strip()]:
                await cur.execute(stmt)
        await con.commit()

async def db_execute(q, *p):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as con:
        async with con.cursor() as cur:
            await cur.execute(q.replace("?", "%s"), p)
        await con.commit()

async def db_fetchone(q, *p):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as con:
        con.row_factory = dict_row
        async with con.cursor() as cur:
            await cur.execute(q.replace("?", "%s"), p)
            return await cur.fetchone()

async def db_fetchall(q, *p):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as con:
        con.row_factory = dict_row
        async with con.cursor() as cur:
            await cur.execute(q.replace("?", "%s"), p)
            return await cur.fetchall()

# ---------------------------
# UTILS
# ---------------------------
def gen_state(n=32):
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

def build_oauth_url_setup(state: str):
    redirect_uri = f"{BASE_URL}/twitch/setup/callback"
    scope = "+".join(SCOPE_SETUP)
    return (
        f"{TW_OAUTH_AUTH}?client_id={OAUTH_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code&scope={scope}"
        f"&state={state}&force_verify=false"
    )

def build_oauth_url_user(state: str, slug: str, broadcaster_id: str):
    redirect_uri = f"{BASE_URL}/twitch/callback"
    scope = "+".join(SCOPE_USER)
    return (
        f"{TW_OAUTH_AUTH}"
        f"?client_id={OAUTH_CLIENT_ID_VIEWER}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&state={state}"
        f"&force_verify=false"
    )

async def send_telegram(uid, text, markdown=False):
    try:
        payload = {"chat_id": uid, "text": text, "disable_web_page_preview": True}
        if markdown:
            payload["parse_mode"] = "Markdown"
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception:
        pass

# ---------------------------
# TOKEN REFRESH
# ---------------------------
async def refresh_twitch_token(b):
    if not b.get("refresh_token"):
        return b["access_token"]
    elapsed = time.time() - b["token_obtained_at"]
    if elapsed < max(0, b["token_expires_in"] - 86400):
        return b["access_token"]
    try:
        data = requests.post(TW_OAUTH_TOKEN, data={
            "grant_type": "refresh_token",
            "refresh_token": b["refresh_token"],
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET
        }, timeout=15).json()
    except Exception:
        return b["access_token"]

    if "access_token" in data:
        new_token = data["access_token"]
        new_refresh = data.get("refresh_token", b["refresh_token"])
        expires_in = data.get("expires_in", b["token_expires_in"])
        token_time = int(time.time())
        await db_execute(
            "UPDATE broadcasters SET access_token=%s, refresh_token=%s, token_obtained_at=%s, token_expires_in=%s WHERE broadcaster_id=%s",
            new_token, new_refresh, token_time, expires_in, b["broadcaster_id"]
        )
        return new_token
    else:
        return b["access_token"]

# ---------------------------
# LIMPIEZA AUTOMÁTICA
# ---------------------------
async def cleanup_task():
    while True:
        try:
            cutoff = int(time.time()) - 86400
            await db_execute("DELETE FROM oauth_states WHERE created_at < %s", cutoff)
            await db_execute("DELETE FROM oauth_pkce WHERE created_at < %s", cutoff)
            # 🧹 Limpieza de usuarios inactivos (>90 días sin check)
            inactive_cutoff = int(time.time()) - (90 * 24 * 3600)
            await db_execute(
                "DELETE FROM telegram_users WHERE last_check IS NOT NULL AND last_check < %s",
                inactive_cutoff
            )
        except Exception:
            pass
        await asyncio.sleep(86400)

async def token_refresher_task():
    while True:
        try:
            broadcasters = await db_fetchall("SELECT * FROM broadcasters")
            if broadcasters:
                for b in broadcasters:
                    await refresh_twitch_token(b)
        except Exception:
            pass
        await asyncio.sleep(21600)
# ---------------------------
# COMANDOS
# ---------------------------
async def setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return await update.message.reply_text("⚠️ Usa este comando en privado.")
    u = update.effective_user
    state = gen_state()
    await db_execute(
        "INSERT INTO oauth_states(state,telegram_id,purpose,created_at) VALUES(%s,%s,%s,%s)",
        state, u.id, "broadcaster_setup", int(time.time())
    )
    url = build_oauth_url_setup(state)
    await update.message.reply_text(
        f"💜 [Haz clic aquí para vincular tu canal de Twitch]({url})\n\n"
        "Tras vincularlo, recibirás tus comandos disponibles.",
        parse_mode="Markdown", disable_web_page_preview=True
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user = update.effective_user

    # Obtener slug (nombre del streamer)
    args = context.args if context.args else []
    if not args and update.message and update.message.text:
        parts = update.message.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            args = [parts[1]]

    if not args:
        return await update.message.reply_text("⚠️ Usa el enlace del bot del streamer para iniciar la vinculación.")

    slug = args[0].strip().lower()

    # Buscar broadcaster por slug
    b = await db_fetchone("SELECT * FROM broadcasters WHERE slug=%s", slug)
    if not b:
        return await update.message.reply_text("❌ Este streamer no tiene vinculación activa.")

    # Guardar estado para el viewer
    state = gen_state()
    await db_execute(
        "INSERT INTO oauth_states(state,telegram_id,purpose,created_at,broadcaster_id) VALUES(%s,%s,%s,%s,%s)",
        state, user.id, "user_link", int(time.time()), b["broadcaster_id"]
    )

    # Generar PKCE (code_verifier + code_challenge)
    code_verifier = gen_state(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )

    await db_execute(
        "INSERT INTO oauth_pkce(state, code_verifier, created_at) VALUES(%s,%s,%s) "
        "ON CONFLICT (state) DO UPDATE SET code_verifier=%s, created_at=%s",
        state, code_verifier, int(time.time()), code_verifier, int(time.time())
    )

    # Generar URL de autorización correctamente
    import urllib.parse
    params = {
        "client_id": OAUTH_CLIENT_ID_VIEWER,
        "redirect_uri": f"{BASE_URL}/twitch/callback",
        "response_type": "code",
        "scope": "user:read:email",
        "state": state,
        "force_verify": "false",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256"
    }
    url = f"{TW_OAUTH_AUTH}?{urllib.parse.urlencode(params)}"

    # Enviar al usuario
    await update.message.reply_text(
        f"💜 [Haz clic aquí para vincular tu Twitch y comprobar si eres sub de *{slug}*]({url})\n\n"
        "🎟️ Tras completar el login, recibirás el resultado aquí en Telegram y podrás acceder a uno de los grupos.",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    logging.info(f"🔗 /start por @{user.username or user.id} → {url}")
# ---------------------------
# CHECKME
# ---------------------------
async def checkme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    tuser = await db_fetchone("SELECT * FROM telegram_users WHERE telegram_id=%s", u.id)
    if not tuser or not tuser["linked_twitch_id"]:
        return await update.message.reply_text("⚠️ No tienes ninguna cuenta de Twitch vinculada. Usa /start con el enlace del streamer primero.")

    row = await db_fetchone("SELECT broadcaster_id FROM oauth_states WHERE telegram_id=%s AND purpose='user_link' ORDER BY created_at DESC LIMIT 1", u.id)
    if not row:
        return await update.message.reply_text("❌ No se encontró canal asociado. Usa el enlace del streamer para verificarte.")
    b = await db_fetchone("SELECT * FROM broadcasters WHERE broadcaster_id=%s", row["broadcaster_id"])
    if not b:
        return await update.message.reply_text("❌ Canal no encontrado. Pide al streamer que haga /setup de nuevo.")

    b["access_token"] = await refresh_twitch_token(b)
    headers = {"Authorization": f"Bearer {b['access_token']}", "Client-Id": OAUTH_CLIENT_ID}
    r = requests.get(f"{TW_API}/subscriptions/user", headers=headers, params={
        "broadcaster_id": b["broadcaster_id"], "user_id": tuser["linked_twitch_id"]
    }, timeout=15)
    data = r.json().get("data")
    if r.status_code == 200 and data:
        sub = data[0]
        tier = sub.get("tier", "1")[-1]
        ends_at = sub.get("ends_at")
        msg = f"💜 Sigues siendo suscriptor de *{b['slug']}* 🎟️ (Nivel {tier})"
        if ends_at:
            end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
            remaining = (end_dt - datetime.now(timezone.utc)).days
            msg += f"\n📅 Tu suscripción termina el {end_dt.strftime('%d/%m/%Y')} ({remaining} días restantes)"
        if b.get("invite_link"):
            msg += f"\n\nAcceso al grupo premium:\n{b['invite_link']}"
        await update.message.reply_text(msg, parse_mode="Markdown")
      # 🆕 Actualizar fecha de última comprobación
        await db_execute("UPDATE telegram_users SET last_check=%s WHERE telegram_id=%s", int(time.time()), u.id)
    else:
        link = b.get("free_invite_link") or "—"
        await update.message.reply_text(f"😔 No apareces como suscriptor de *{b['slug']}*.\nÚnete al grupo gratuito:\n{link}", parse_mode="Markdown")

# ---------------------------
# SETGROUP Y SETFREEGROUP
# ---------------------------
async def setgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    u = update.effective_user
    b = await db_fetchone("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", u.id)
    if not b:
        return await update.message.reply_text("❌ Primero usa /setup en privado.")
    link = await context.bot.create_chat_invite_link(update.effective_chat.id)
    await db_execute("UPDATE broadcasters SET group_id=%s, invite_link=%s WHERE broadcaster_id=%s",
                     update.effective_chat.id, link.invite_link, b["broadcaster_id"])
    await update.message.reply_text("✅ Grupo premium vinculado correctamente.")
    bot_link = f"https://t.me/{BOT_USERNAME}?start={b['slug']}"
    await send_telegram(u.id,
        f"💜 Grupo premium vinculado.\n\n🎥 Canal: {b['slug']}\n🔗 Enlace del bot para tus viewers:\n{bot_link}\n\n"
        "🧹 Usa /auditfull en el grupo premium para limpiar no-suscriptores.\n"
        "⭐ Gestiona excepciones con /privilege_add, /privilege_remove y /privilege_list.",
        markdown=False
    )

async def setfreegroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    u = update.effective_user
    b = await db_fetchone("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", u.id)
    if not b:
        return await update.message.reply_text("❌ Primero usa /setup en privado.")
    me = await context.bot.get_me()
    bot_member = await context.bot.get_chat_member(update.effective_chat.id, me.id)
    if not bot_member.can_invite_users:
        return await update.message.reply_text("⚠️ El bot debe ser *administrador con permiso para invitar* antes de usar este comando.")
    try:
        link = await context.bot.create_chat_invite_link(update.effective_chat.id)
        await db_execute("UPDATE broadcasters SET free_group_id=%s, free_invite_link=%s WHERE broadcaster_id=%s",
                         update.effective_chat.id, link.invite_link, b["broadcaster_id"])
        await update.message.reply_text("✅ Grupo gratuito vinculado correctamente.")
        await send_telegram(u.id, "💬 Grupo gratuito vinculado correctamente.", markdown=False)
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudo crear enlace de invitación.\nDetalles: {e}")

# ---------------------------
# PRIVILEGES
# ---------------------------
async def privilege_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    if not context.args:
        return await update.message.reply_text("❌ Usa: /privilege_add @usuario o nombreTwitch")
    username = context.args[0].lstrip("@").lower()
    b = await db_fetchone("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", u.id)
    if not b:
        return await update.message.reply_text("❌ No tienes un canal vinculado.")
    await db_execute("INSERT INTO privileged_users(broadcaster_id,username) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                     b["broadcaster_id"], username)
    await update.message.reply_text(f"🔒 Usuario @{username} añadido como privilegiado.")

async def privilege_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    if not context.args:
        return await update.message.reply_text("❌ Usa: /privilege_remove @usuario o nombreTwitch")
    username = context.args[0].lstrip("@").lower()
    b = await db_fetchone("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", u.id)
    if not b:
        return await update.message.reply_text("❌ No tienes un canal vinculado.")
    await db_execute("DELETE FROM privileged_users WHERE broadcaster_id=%s AND username=%s",
                     b["broadcaster_id"], username)
    await update.message.reply_text(f"🧹 Usuario @{username} eliminado de privilegios.")

async def privilege_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    b = await db_fetchone("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", u.id)
    if not b:
        return await update.message.reply_text("❌ No tienes un canal vinculado.")
    rows = await db_fetchall("SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"])
    if not rows:
        return await update.message.reply_text("⚙️ No tienes usuarios privilegiados.")
    lista = "\n".join([f"• @{r['username']}" for r in rows])
    await update.message.reply_text(f"🔒 Lista de usuarios privilegiados:\n{lista}")

# ---------------------------
# AUDITFULL
# ---------------------------
async def auditfull_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    chat_id = update.effective_chat.id
    u = update.effective_user
    b = await db_fetchone("SELECT * FROM broadcasters WHERE owner_telegram_id=%s", u.id)
    if not b or not b.get("group_id") or b["group_id"] != chat_id:
        return await update.message.reply_text("⚠️ Este grupo no está vinculado como premium.")
    privileged_db = [r["username"] for r in await db_fetchall(
        "SELECT username FROM privileged_users WHERE broadcaster_id=%s", b["broadcaster_id"]
    )]
    count = 0
    async for member in context.bot.get_chat(chat_id).iter_members():
        if member.user.username and member.user.username.lower() in privileged_db:
            continue
        count += 1
    await update.message.reply_text(f"🧹 Auditoría completada. Usuarios revisados: {count}")
# ---------------------------
# CALLBACKS TWITCH
# ---------------------------
async def twitch_callback_setup(request: web.Request):
    try:
        params = request.rel_url.query
        code = params.get("code")
        state = params.get("state")

        if not code or not state:
            return web.Response(text="❌ Falta código o estado.", status=400)

        # Verificar estado
        row = await db_fetchone("SELECT * FROM oauth_states WHERE state=%s AND purpose='broadcaster_setup'", state)
        if not row:
            return web.Response(text="❌ Estado no válido o expirado.", status=400)

        # Intercambio del code por token (flujo confidencial)
        data = requests.post(
            TW_OAUTH_TOKEN,
            data={
                "client_id": OAUTH_CLIENT_ID,  # Tw-Telegram SubCheck
                "client_secret": OAUTH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{BASE_URL}/twitch/setup/callback"
            },
            timeout=15
        ).json()

        if "access_token" not in data:
            return web.Response(text=f"❌ Error al obtener token: {data}", status=400)

        access_token = data["access_token"]
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in", 0)
        token_time = int(time.time())

        # Obtener datos del broadcaster
        headers = {"Authorization": f"Bearer {access_token}", "Client-Id": OAUTH_CLIENT_ID}
        me = requests.get(f"{TW_API}/users", headers=headers, timeout=15).json()["data"][0]
        slug = me["login"].lower()

        # Guardar canal
        await db_execute("""
            INSERT INTO broadcasters(broadcaster_id, owner_telegram_id, access_token, refresh_token,
                                     token_obtained_at, token_expires_in, slug)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (broadcaster_id)
            DO UPDATE SET access_token=EXCLUDED.access_token,
                          refresh_token=EXCLUDED.refresh_token,
                          token_obtained_at=EXCLUDED.token_obtained_at,
                          token_expires_in=EXCLUDED.token_expires_in,
                          slug=EXCLUDED.slug
        """, me["id"], row["telegram_id"], access_token, refresh_token, token_time, expires_in, slug)

        # Enviar confirmación
        bot_link = f"https://t.me/{BOT_USERNAME}?start={slug}"
        msg = (
            "✅ Canal vinculado correctamente.\n"
            f"🔗 Enlace para tus viewers:\n{bot_link}\n\n"
            "Comandos disponibles:\n"
            "• /setgroup – vincula grupo premium\n"
            "• /setfreegroup – vincula grupo gratuito\n"
            "• /auditfull – limpia no-suscriptores\n"
            "• /privilege_add /privilege_remove /privilege_list – gestiona miembros protegidos"
        )
        await send_telegram(row["telegram_id"], msg, markdown=False)
        return web.Response(text="✅ Canal vinculado correctamente. Ya puedes volver a Telegram.", status=200)

    except Exception as e:
        return web.Response(text=f"❌ Error interno: {e}", status=500)

# ---------------------------
# CALLBACK USER (PKCE)
# ---------------------------
async def twitch_callback_user(request: web.Request):
    try:
        params = request.rel_url.query
        code = params.get("code")
        state = params.get("state")

        if not code or not state:
            return web.Response(text="❌ Falta código o estado.", status=400)

        # Verificar estado
        row = await db_fetchone("SELECT * FROM oauth_states WHERE state=%s AND purpose='user_link'", state)
        if not row:
            return web.Response(text="❌ Estado no válido o expirado.", status=400)

        verifier_row = await db_fetchone("SELECT code_verifier FROM oauth_pkce WHERE state=%s", state)
        code_verifier = verifier_row["code_verifier"] if verifier_row else None
        if not code_verifier:
            return web.Response(text="❌ No se encontró el code_verifier.", status=400)

        # Intercambiar el code del viewer por token (flujo PKCE)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "client_id": OAUTH_CLIENT_ID_VIEWER,
            "grant_type": "authorization_code",
            "redirect_uri": f"{BASE_URL}/twitch/callback",
            "code": code,
            "code_verifier": code_verifier
        }
        import urllib.parse
        encoded_payload = urllib.parse.urlencode(payload)
        
        data = requests.post(TW_OAUTH_TOKEN,headers=headers,data=encoded_payload.encode("utf-8"),timeout=15).json()
      
        if "access_token" not in data:
            return web.Response(text=f"❌ Error al obtener token: {data}", status=400)

        access_token = data["access_token"]

        # Obtener usuario Twitch
        headers_user = {"Authorization": f"Bearer {access_token}", "Client-Id": OAUTH_CLIENT_ID_VIEWER}
        me = requests.get(f"{TW_API}/users", headers=headers_user, timeout=15).json()["data"][0]

        # Guardar Twitch + Telegram
        await db_execute("""
            INSERT INTO twitch_users(twitch_id, login, display_name, email)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (twitch_id)
            DO UPDATE SET login=EXCLUDED.login, display_name=EXCLUDED.display_name, email=EXCLUDED.email
        """, me["id"], me["login"], me["display_name"], me.get("email", ""))

        await db_execute("""
            INSERT INTO telegram_users(telegram_id, username, linked_twitch_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id)
            DO UPDATE SET username=EXCLUDED.username, linked_twitch_id=EXCLUDED.linked_twitch_id
        """, row["telegram_id"], "", me["id"])

        # Verificar subs
        b = await db_fetchone("SELECT * FROM broadcasters WHERE broadcaster_id=%s", row["broadcaster_id"])
        if not b:
            return web.Response(text="❌ Canal no encontrado. Pide al streamer que haga /setup.", status=400)

        b["access_token"] = await refresh_twitch_token(b)
        headers_b = {"Authorization": f"Bearer {b['access_token']}", "Client-Id": OAUTH_CLIENT_ID}
        r = requests.get(f"{TW_API}/subscriptions/user", headers=headers_b, params={
            "broadcaster_id": b["broadcaster_id"], "user_id": me["id"]
        }, timeout=15)
        data_subs = r.json().get("data")

        if r.status_code == 200 and data_subs:
            sub = data_subs[0]
            tier = sub.get("tier", "1")[-1]
            ends_at = sub.get("ends_at")
            msg = f"💜 *{me['display_name']}*, eres suscriptor de *{b['slug']}* 🎟️ (Nivel {tier})"
            if ends_at:
                end_dt = datetime.fromisoformat(ends_at.replace('Z', '+00:00'))
                remaining = (end_dt - datetime.now(timezone.utc)).days
                msg += f"\n📅 Tu suscripción termina el {end_dt.strftime('%d/%m/%Y')} ({remaining} días restantes)"
            if b.get("invite_link"):
                msg += f"\n\nÚnete al grupo premium:\n{b['invite_link']}\n\nUsa /checkme cuando quieras."
        else:
            free = b.get("free_invite_link") or "—"
            msg = (
                f"😔 *{me['display_name']}*, no apareces como suscriptor de *{b['slug']}*.\n\n"
                f"Puedes unirte al grupo gratuito:\n{free}\n\nCuando te suscribas, usa /checkme para verificar de nuevo."
            )

        await send_telegram(row["telegram_id"], msg, markdown=True)
        return web.Response(text="✅ Vinculación completada. Puedes volver a Telegram.", status=200)

    except Exception as e:
        return web.Response(text=f"❌ Error interno: {e}", status=500)

# ---------------------------
# WEBHOOK Y MAIN
# ---------------------------
async def handle_webhook(request: web.Request):
    data = await request.json()
    update = Update.de_json(data, request.app["bot"].bot)
    await request.app["bot"].process_update(update)
    return web.Response(text="OK")

async def test(request):
    return web.Response(text="✅ Bot operativo correctamente.", status=200)

async def main():
    await db_init()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("setup", setup_cmd))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("checkme", checkme))
    app.add_handler(CommandHandler("setgroup", setgroup_cmd))
    app.add_handler(CommandHandler("setfreegroup", setfreegroup_cmd))
    app.add_handler(CommandHandler("privilege_add", privilege_add))
    app.add_handler(CommandHandler("privilege_remove", privilege_remove))
    app.add_handler(CommandHandler("privilege_list", privilege_list))
    app.add_handler(CommandHandler("auditfull", auditfull_cmd))

    webhook_url = f"{BASE_URL}/webhook"
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                     params={"url": webhook_url}, timeout=10)
    except Exception:
        pass

    web_app = web.Application()
    web_app["bot"] = app
    web_app.router.add_post("/webhook", handle_webhook)
    web_app.router.add_get("/twitch/setup/callback", twitch_callback_setup)
    web_app.router.add_get("/twitch/callback", twitch_callback_user)
    web_app.router.add_get("/test", test)
    web_app.router.add_get("/", test)

    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    asyncio.create_task(cleanup_task())
    asyncio.create_task(token_refresher_task())

    await app.initialize()
    await app.start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
