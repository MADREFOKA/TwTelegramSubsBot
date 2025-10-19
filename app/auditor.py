# app/auditor.py
import os
import aiohttp
from typing import Tuple
from . import db
from .oauth import ensure_valid_token

TW_HELIX_URL = "https://api.twitch.tv/helix"

async def _fetch_current_subscribers(slug: str, twitch_user_id: str, access_token: str) -> set[str]:
    """Obtiene la lista actual de suscriptores activos desde Twitch."""
    headers = {
        "Client-Id": os.environ["TWITCH_CLIENT_ID"],
        "Authorization": f"Bearer {access_token}",
    }
    subs = set()
    url = f"{TW_HELIX_URL}/subscriptions?broadcaster_id={twitch_user_id}"

    async with aiohttp.ClientSession() as session:
        while url:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
            for item in data.get("data", []):
                subs.add(item["user_id"])
            cursor = data.get("pagination", {}).get("cursor")
            url = f"{TW_HELIX_URL}/subscriptions?broadcaster_id={twitch_user_id}&after={cursor}" if cursor else None

    return subs

async def run_audit_for_slug(slug: str, tg_app) -> Tuple[int, int]:
    """Audita suscriptores y limpia usuarios no suscritos."""
    st = await db.get_streamer_by_slug(slug)
    if not st:
        raise RuntimeError(f"Streamer not found {slug}")

    premium_group_id = st["premium_group_id"]
    free_group_id = st["free_group_id"]
    twitch_user_id = st["twitch_user_id"]

    if not premium_group_id or not twitch_user_id:
        await db.record_audit(slug, 0, 0, "Missing premium_group_id or twitch_user_id")
        return (0, 0)

    access = await ensure_valid_token(slug)
    subs_user_ids = await _fetch_current_subscribers(slug, twitch_user_id, access)

    # 🔧 Aquí debes reemplazar con tu lógica real de mapeo Telegram ↔ Twitch
    twitch_map = {}       # telegram_id -> twitch_user_id
    group_members = set() # IDs reales del grupo (rellena según tu implementación)
    removed = 0
    kept = 0

    # Detectar no suscritos
    to_remove = []
    for tg_id in group_members:
        tuid = twitch_map.get(tg_id)
        if not tuid or tuid not in subs_user_ids:
            to_remove.append(tg_id)

    # Expulsar y mover
    for tg_id in to_remove:
        try:
            await tg_app.bot.ban_chat_member(premium_group_id, tg_id)
            await tg_app.bot.unban_chat_member(premium_group_id, tg_id)
            removed += 1
            if free_group_id:
                await tg_app.bot.send_message(tg_id, "Tu suscripción expiró. Únete al grupo gratuito aquí: [link]")
        except Exception:
            pass

    kept = len(group_members) - removed
    await db.record_audit(slug, removed, kept)
    return (removed, kept)
