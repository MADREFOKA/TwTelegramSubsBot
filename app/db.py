import asyncio, time, logging
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row
import os

class Database:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.pool = AsyncConnectionPool(self.db_url, open=False)
        self.CREATE_SQL = """
        CREATE TABLE IF NOT EXISTS telegram_users (
          telegram_id BIGINT PRIMARY KEY,
          username TEXT,
          linked_twitch_id TEXT,
          last_check INTEGER
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
        """

    async def init(self):
        logging.info("🔄 Inicializando base de datos NeonDB...")
        await self.pool.open()
        async with self.pool.connection() as con:
            async with con.cursor() as cur:
                for stmt in [x.strip() for x in self.CREATE_SQL.split(";") if x.strip()]:
                    await cur.execute(stmt)
            await con.commit()
        logging.info("✅ Tablas verificadas y conexión establecida.")

    async def execute(self, query, *params):
        async with self.pool.connection() as con:
            async with con.cursor() as cur:
                await cur.execute(query.replace("?", "%s"), params)
            await con.commit()

    async def one(self, query, *params):
        async with self.pool.connection() as con:
            con.row_factory = dict_row
            async with con.cursor() as cur:
                await cur.execute(query.replace("?", "%s"), params)
                return await cur.fetchone()

    async def all(self, query, *params):
        async with self.pool.connection() as con:
            con.row_factory = dict_row
            async with con.cursor() as cur:
                await cur.execute(query.replace("?", "%s"), params)
                return await cur.fetchall()
