# app/oauth.py
import os
import aiohttp
from datetime import datetime, timezone, timedelta
from . import db
from .security import encrypt_token, decrypt_token

TW_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TW_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
TW_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
REFRESH_THRESHOLD = timedelta(minutes=10)

async def ensure_valid_token(slug: str) -> str:
    """
    Verifica si el token aún es válido.
    Si expira en menos de 10 minutos, se refresca automáticamente.
    Devuelve siempre un access_token válido.
    """
    rec = await db.get_tokens(slug)
    if not rec:
        raise RuntimeError(f"No tokens stored for slug={slug}")

    access_enc, refresh_enc, expires_at = rec["access_token_enc"], rec["refresh_token_enc"], rec["expires_at"]
    access = decrypt_token(access_enc)
    refresh = decrypt_token(refresh_enc)
    now = datetime.now(timezone.utc)

    if expires_at - now > REFRESH_THRESHOLD:
        return access  # Token aún válido

    # Refrescar token
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": TW_CLIENT_ID,
        "client_secret": TW_CLIENT_SECRET,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(TW_TOKEN_URL, data=payload) as resp:
            data = await resp.json()

    if "access_token" not in data:
        raise RuntimeError(f"Twitch token refresh failed for {slug}: {data}")

    new_access = data["access_token"]
    new_refresh = data.get("refresh_token", refresh)
    expires_in = int(data.get("expires_in", 3600))
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    await db.save_tokens(
        slug,
        encrypt_token(new_access),
        encrypt_token(new_refresh),
        new_expires_at
    )

    return new_access
