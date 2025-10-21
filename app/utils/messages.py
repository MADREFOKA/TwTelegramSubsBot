# app/utils/messages.py
from telegram import InlineKeyboardMarkup, InlineKeyboardButton


# ✅ Mensaje tras el /setup del streamer
def setup_success(name, bot_username, slug, dash_link):
    msg = (
        f"🎥 Canal: {name}\n"
        f"🔗 Enlace viewers:\nhttps://t.me/{bot_username}?start={slug}\n\n"
        f"⚙️ Configuración:\n"
        f"/setgroup – Vincula grupo premium\n"
        f"/setfreegroup – Vincula grupo gratuito\n"
        f"/auditnow – Limpia no-suscriptores\n"
        f"/priv_add @usuario – Añade a whitelist\n"
        f"/priv_remove @usuario – Quita de whitelist\n"
        f"/priv_list – Lista whitelist\n"
        f"/regendashlink – Regenera enlace dashboard"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Abrir Dashboard", url=dash_link),
            InlineKeyboardButton("🧭 Guía de uso", callback_data="show_guide")
        ]
    ])

    return msg, keyboard


# 👑 Mensaje si el usuario es el propietario del canal
def viewer_is_owner(name, slug, invite_link):
    msg = (
        f"👑 Hola {name}!\n\n"
        f"Eres el propietario del canal *{slug}*.\n"
        f"No necesitas verificarte como sub. 😊"
    )
    return msg, None


# 💜 Mensaje si el usuario ES suscriptor
def viewer_is_sub(name, slug, renew_date, days, invite_link):
    msg = (
        f"💜 ¡Gracias por tu apoyo, {name}!\n\n"
        f"Eres *suscriptor activo* de *{slug}*.\n"
        f"Tu renovación será el *{renew_date}* (en {days} días)."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Entrar al grupo premium", url=invite_link)]
    ]) if invite_link else None
    return msg, keyboard


# ⚪ Mensaje si el usuario NO es suscriptor
def viewer_not_sub(name, slug, free_link):
    msg = (
        f"🤍 Hola {name}.\n\n"
        f"No eres suscriptor activo de *{slug}*.\n"
        f"Puedes seguir el canal y participar en el grupo gratuito."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆓 Entrar al grupo gratuito", url=free_link)]
    ]) if free_link else None
    return msg, keyboard
