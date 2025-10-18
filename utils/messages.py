# app/utils/messages.py

def setup_success(display_name: str, bot_username: str, slug: str) -> str:
    return (
        "✅ <b>Canal vinculado correctamente</b>\n\n"
        f"🎥 Canal: <b>{display_name}</b>\n"
        f"🔗 Enlace viewers:\nhttps://t.me/{bot_username}?start={slug}\n\n"
        "⚙️ <b>Configuración:</b>\n"
        "<code>/setgroup</code> – Vincula grupo premium\n"
        "<code>/setfreegroup</code> – Vincula grupo gratuito\n"
        "<code>/auditnow</code> – Limpia no-suscriptores\n"
        "<code>/priv_add @usuario</code> – Añade a whitelist\n"
        "<code>/priv_remove @usuario</code> – Quita de whitelist\n"
        "<code>/priv_list</code> – Lista whitelist\n"
    )

def viewer_is_owner(user: str, streamer: str, invite: str | None) -> str:
    if invite:
        return (
            f"👑 <b>{user}</b>, eres el propietario del canal <b>{streamer}</b>.\n\n"
            f"Acceso automático al grupo premium:\n{invite}"
        )
    return (
        f"👑 <b>{user}</b>, eres el propietario del canal <b>{streamer}</b>.\n\n"
        "Aún no has vinculado tu grupo premium. Usa <code>/setgroup</code> en el grupo."
    )

def viewer_is_sub(user: str, streamer: str, renew: str, days_left: int, invite: str | None) -> str:
    plural = "s" if days_left != 1 else ""
    base = (
        f"💜 <b>{user}</b>, eres suscriptor de <b>{streamer}</b> 🎟️\n"
        f"🗓️ Tu suscripción se renueva el <b>{renew}</b> "
        f"(<i>{days_left} día{plural} restante{plural}</i>)\n\n"
    )
    if invite:
        return base + f"Aquí tienes el grupo de subs:\n{invite}"
    return base + "El streamer aún no ha vinculado su grupo de subs."

def viewer_not_sub(user: str, streamer: str, free_link: str | None) -> str:
    msg = (
        f"😔 <b>{user}</b>, no apareces como suscriptor de <b>{streamer}</b>.\n\n"
        f"Si te has suscrito recientemente, espera unos minutos y usa "
        f"<code>/checkme {streamer}</code> para comprobarlo de nuevo.\n\n"
    )
    if free_link:
        msg += f"🎟️ Aquí tienes el grupo gratuito:\n{free_link}"
    return msg

def group_premium_linked(bot_username: str, slug: str) -> str:
    return (
        "✅ Grupo premium vinculado correctamente.\n\n"
        "💜 Grupo premium vinculado.\n"
        f"🔗 Enlace viewers:\nhttps://t.me/{bot_username}?start={slug}"
    )
