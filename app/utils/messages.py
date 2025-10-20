# app/utils/messages.py
# ───────────────────────────────
# ⚙️ CONFIGURACIÓN DEL STREAMER
# ───────────────────────────────
def setup_success(display_name: str, bot_username: str, slug: str, dash_link: str) -> str:
    """Mensaje completo y profesional cuando el streamer vincula su canal."""
    return (
        f"✅ <b>Canal vinculado correctamente</b>\n\n"
        f"🎥 <b>Canal:</b> {display_name}\n"
        f"🔗 <b>Enlace viewers:</b>\n"
        f"https://t.me/{bot_username}?start={slug}\n\n"
        f"📊 <b>Dashboard privado:</b>\n"
        f"{dash_link}\n\n"
        f"⚙️ <b>Configuración:</b>\n"
        f"<code>/setgroup</code> – Vincula grupo premium\n"
        f"<code>/setfreegroup</code> – Vincula grupo gratuito\n"
        f"<code>/auditnow</code> – Limpia no-suscriptores\n"
        f"<code>/priv_add @usuario</code> – Añade a whitelist\n"
        f"<code>/priv_remove @usuario</code> – Quita de whitelist\n"
        f"<code>/priv_list</code> – Lista whitelist\n"
        f"<code>/regendashlink</code> – Regenera enlace dashboard"
    )

def group_premium_linked(bot_username: str, slug: str) -> str:
    """Confirmación al vincular el grupo premium."""
    return (
        "✅ <b>Grupo premium vinculado correctamente.</b>\n\n"
        f"🔗 <b>Enlace viewers:</b>\n"
        f"https://t.me/{bot_username}?start={slug}"
    )

def free_group_linked() -> str:
    """Confirmación al vincular el grupo gratuito."""
    return "🎁 <b>Grupo gratuito vinculado correctamente.</b>"

# ───────────────────────────────
# 👥 MENSAJES PARA VIEWERS
# ───────────────────────────────
def viewer_is_owner(user: str, streamer: str, invite: str | None) -> str:
    """Viewer es el propietario del canal."""
    if invite:
        return (
            f"👑 <b>{user}</b>, eres el propietario del canal <b>{streamer}</b>.\n\n"
            f"Acceso automático al grupo premium:\n"
            f"<a href=\"{invite}\">{invite}</a>"
        )
    return (
        f"👑 <b>{user}</b>, eres el propietario del canal <b>{streamer}</b>.\n\n"
        "Aún no has vinculado tu grupo premium. Usa <code>/setgroup</code> en el grupo."
    )

def viewer_is_sub(user: str, streamer: str, renew: str, days_left: int, invite: str | None) -> str:
    """Viewer es suscriptor del streamer."""
    plural = "s" if days_left != 1 else ""
    base = (
        f"💜 <b>{user}</b>, eres suscriptor de <b>{streamer}</b> 🎟️\n"
        f"🗓️ <b>Tu suscripción se renueva el:</b> <b>{renew}</b> "
        f"(<i>{days_left} día{plural} restante{plural}</i>)\n\n"
    )
    if invite:
        return base + f"🎁 <b>Aquí tienes el grupo de subs:</b>\n<a href=\"{invite}\">{invite}</a>"
    return base + "El streamer aún no ha vinculado su grupo de subs."

def viewer_not_sub(user: str, streamer: str, free_link: str | None) -> str:
    """Viewer no es suscriptor."""
    msg = (
        f"😔 <b>{user}</b>, no apareces como suscriptor de <b>{streamer}</b>.\n\n"
        f"Si te has suscrito recientemente, espera unos minutos y usa "
        f"<code>/checkme {streamer}</code> para comprobarlo de nuevo.\n\n"
    )
    if free_link:
        msg += f"🎟️ <b>Aquí tienes el grupo gratuito:</b>\n<a href=\"{free_link}\">{free_link}</a>"
    else:
        msg += "⚠️ Este streamer aún no tiene grupo gratuito vinculado."
    return msg

# ───────────────────────────────
# 🧹 AUDITORÍAS
# ───────────────────────────────
def audit_start() -> str:
    """Inicio de auditoría manual."""
    return "⏳ <b>Ejecutando auditoría de suscriptores...</b>"

def audit_done() -> str:
    """Finalización de auditoría manual."""
    return (
        "✅ <b>Limpieza completada.</b>\n\n"
        "Puedes ver los resultados en tu dashboard."
    )

def audit_auto_done() -> str:
    """Auditoría automática semanal completada."""
    return (
        "🧹 <b>Auditoría automática completada.</b>\n\n"
        "Revisa tu dashboard para ver las estadísticas actualizadas."
    )


def audit_failed() -> str:
    """Error durante la auditoría."""
    return (
        "⚠️ <b>Error durante la auditoría.</b>\n\n"
        "Intenta de nuevo más tarde o revisa tu conexión con Twitch."
    )

# ───────────────────────────────
# ⚠️ ERRORES Y SISTEMA
# ───────────────────────────────
def not_streamer() -> str:
    """Comando restringido a streamers."""
    return (
        "🚫 <b>Este comando es solo para streamers verificados.</b>\n\n"
        "Si eres viewer, puedes usar tu enlace personal para vincularte al canal."
    )


def missing_group() -> str:
    """Streamer intenta usar un comando sin haber vinculado grupo."""
    return "⚠️ <b>Aún no has configurado tu grupo premium.</b>\nUsa <code>/setgroup</code> en el grupo correspondiente."


def token_expired() -> str:
    """Token OAuth de Twitch caducado."""
    return (
        "🔒 <b>Token expirado.</b>\n\n"
        "Por favor, vuelve a vincular tu cuenta de Twitch usando <code>/setup</code>."
    )

# ───────────────────────────────
# 📊 DASHBOARD
# ───────────────────────────────
def dashboard_link(dashboard_url: str) -> str:
    """Mensaje con el link directo al dashboard del streamer."""
    return (
        "📊 <b>Tu dashboard personal está listo:</b>\n\n"
        f"👉 <a href=\"{dashboard_url}\">{dashboard_url}</a>\n\n"
        "Desde ahí podrás ver tus auditorías, suscriptores activos, y expulsados semanales."
    )
