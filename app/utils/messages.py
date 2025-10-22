# app/utils/messages.py
from telegram import InlineKeyboardMarkup, InlineKeyboardButton


# ✅ Mensaje tras el /setup del streamer
def setup_success(name, bot_username, slug, dash_link):
    msg = (
        f"🎥 Canal: <b>{name}</b>\n\n"
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
        f"Eres el propietario del canal <b>{slug}</b>.\n"
        f"No necesitas verificarte como sub. 😊"
    )
    return msg, None

# 💜 Mensaje si el usuario ES suscriptor
def viewer_is_sub(name, slug, invite_link):
    msg = (
        f"💜 ¡Gracias por tu apoyo, {name}!\n\n"
        f"Eres <b>suscriptor</b> de <b>{slug}</b>.\n"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Entrar al grupo premium", url=invite_link)]
    ]) if invite_link else None
    return msg, keyboard

# ⚪ Mensaje si el usuario NO es suscriptor
def viewer_not_sub(name, slug, free_link):
    msg = (
        f"🤍 Hola {name}.\n\n"
        f"No eres suscriptor activo de <b>{slug}</b>.\n"
        f"Puedes seguir el canal y participar en el grupo gratuito."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆓 Entrar al grupo gratuito", url=free_link)]
    ]) if free_link else None
    return msg, keyboard
    
# Guia de uso del bot
def guide_message() -> str:
    return (
        "<b>📘 Guía rápida de uso</b>\n\n"
        "1️⃣ Añade al bot como admin al grupo y usa <code>/setgroup</code> en tu grupo PREMIUM para vincularlo.\n"
        "2️⃣ (Opcional) Usa <code>/setfreegroup</code> en tu grupo GRATUITO para no-suscriptores.\n"
        "3️⃣ Comparte tu enlace de viewers (aparece tras hacer /setup).\n"
        "4️⃣ Los viewers hacen <code>/start tunick</code> o usan tu link para vincular Twitch.\n"
        "5️⃣ Las auditorías/expulsiones son semanales, puedes hacerlas con <code>/auditnow</code> o con 'Forzar auditoría' del Dashboard.\n"
        "6️⃣ Gestiona tu whitelist con:\n"
        "   <code>/priv_add @usuario</code>, <code>/priv_remove @usuario</code>, <code>/priv_list</code>.\n\n"
        "<b>ℹ️ Nota:</b> Las estadísticas y resultados se ven solo en el Dashboard."
    )
