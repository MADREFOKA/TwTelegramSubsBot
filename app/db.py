# app/db.py
import psycopg
from psycopg.rows import dict_row
import logging

class Database:
    def __init__(self, url: str):
        self.url = url

    async def init(self):
        """Crea tablas necesarias para el sistema multi-streamer"""
        CREATE_SQL = """
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

        CREATE TABLE IF NOT EXISTS privileged_users (
          broadcaster_id TEXT,
          username TEXT,
          PRIMARY KEY (broadcaster_id, username)
        );

        CREATE INDEX IF NOT EXISTS idx_broadcaster_slug ON broadcasters(slug);
        CREATE INDEX IF NOT EXISTS idx_oauth_telegram ON oauth_states(telegram_id);
        """

        async with await psycopg.AsyncConnection.connect(self.url) as con:
            async with con.cursor() as cur:
                for stmt in [x.strip() for x in CREATE_SQL.split(";") if x.strip()]:
                    await cur.execute(stmt)
            await con.commit()
        logging.info("🗄️ Tablas creadas/verificadas correctamente.")

    async def exec(self, query, *params):
        async with await psycopg.AsyncConnection.connect(self.url) as con:
            async with con.cursor() as cur:
                await cur.execute(query.replace("?", "%s"), params)
            await con.commit()

    async def one(self, query, *params):
        async with await psycopg.AsyncConnection.connect(self.url) as con:
            con.row_factory = dict_row
            async with con.cursor() as cur:
                await cur.execute(query.replace("?", "%s"), params)
                return await cur.fetchone()

    async def all(self, query, *params):
        async with await psycopg.AsyncConnection.connect(self.url) as con:
            con.row_factory = dict_row
            async with con.cursor() as cur:
                await cur.execute(query.replace("?", "%s"), params)
                return await cur.fetchall()
