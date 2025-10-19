# app/db.py
import asyncpg
from datetime import datetime, timezone

_pool = None

async def init_pool(dsn: str):
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn, max_inactive_connection_lifetime=60)

def pool():
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool

# --- STREAMERS ---
async def upsert_streamer(slug: str, twitch_user_id: str, telegram_user_id: int, dashboard_token: str):
    q = """
    INSERT INTO streamers (slug, twitch_user_id, telegram_user_id, dashboard_token)
    VALUES ($1,$2,$3,$4)
    ON CONFLICT (slug)
    DO UPDATE SET twitch_user_id=EXCLUDED.twitch_user_id,
                  telegram_user_id=EXCLUDED.telegram_user_id,
                  dashboard_token=EXCLUDED.dashboard_token,
                  updated_at=NOW()
    """
    async with pool().acquire() as con:
        await con.execute(q, slug, twitch_user_id, telegram_user_id, dashboard_token)

async def get_streamer_by_slug(slug: str):
    q = "SELECT * FROM streamers WHERE slug=$1"
    async with pool().acquire() as con:
        return await con.fetchrow(q, slug)

async def get_streamer_by_telegram(telegram_user_id: int):
    q = "SELECT * FROM streamers WHERE telegram_user_id=$1"
    async with pool().acquire() as con:
        return await con.fetchrow(q, telegram_user_id)

async def set_groups(slug: str, premium_group_id: int | None, free_group_id: int | None):
    q = """
    UPDATE streamers
    SET premium_group_id=COALESCE($2, premium_group_id),
        free_group_id=COALESCE($3, free_group_id),
        updated_at=NOW()
    WHERE slug=$1
    """
    async with pool().acquire() as con:
        await con.execute(q, slug, premium_group_id, free_group_id)

async def list_all_slugs():
    q = "SELECT slug FROM streamers"
    async with pool().acquire() as con:
        rows = await con.fetch(q)
    return [r["slug"] for r in rows]

# --- TOKENS ---
async def save_tokens(slug: str, access_token_enc: str, refresh_token_enc: str, expires_at_utc: datetime):
    q = """
    INSERT INTO twitch_tokens(slug, access_token_enc, refresh_token_enc, expires_at, updated_at)
    VALUES ($1,$2,$3,$4,NOW())
    ON CONFLICT (slug)
    DO UPDATE SET access_token_enc=EXCLUDED.access_token_enc,
                  refresh_token_enc=EXCLUDED.refresh_token_enc,
                  expires_at=EXCLUDED.expires_at,
                  updated_at=NOW()
    """
    async with pool().acquire() as con:
        await con.execute(q, slug, access_token_enc, refresh_token_enc, expires_at_utc.replace(tzinfo=timezone.utc))

async def get_tokens(slug: str):
    q = "SELECT access_token_enc, refresh_token_enc, expires_at FROM twitch_tokens WHERE slug=$1"
    async with pool().acquire() as con:
        return await con.fetchrow(q, slug)

# --- AUDITS ---
async def record_audit(slug: str, removed: int, kept: int, notes: str | None = None):
    q = "INSERT INTO audits(slug, removed_count, kept_count, notes) VALUES ($1,$2,$3,$4)"
    async with pool().acquire() as con:
        await con.execute(q, slug, removed, kept, notes)

async def get_latest_audit_stats(slug: str, limit: int = 30):
    q = """
    SELECT run_at, removed_count, kept_count
    FROM audits WHERE slug=$1
    ORDER BY run_at DESC
    LIMIT $2
    """
    async with pool().acquire() as con:
        return await con.fetch(q, slug, limit)
