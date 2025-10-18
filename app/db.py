# app/db.py
import os
import logging
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

class Database:
    def __init__(self):
        self.url = os.getenv("DATABASE_URL")
        if not self.url:
            raise ValueError("❌ Falta DATABASE_URL")

        # Pool de conexiones persistente
        self.pool = AsyncConnectionPool(
            conninfo=self.url,
            min_size=1,
            max_size=5,
            max_idle=30,
            timeout=10,
            open=False  # lo abrimos manualmente en init()
        )

    async def init(self):
        """Inicializa el pool y verifica el esquema."""
        await self.pool.open()
        logging.info("🗄️ Pool de conexiones abierto correctamente.")

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
          slug TEXT,
          dashboard_token TEXT
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

        async with self.pool.connection() as con:
            async with con.cursor() as cur:
                for stmt in [s.strip() for s in CREATE_SQL.split(";") if s.strip()]:
                    await cur.execute(stmt)
            await con.commit()
        logging.info("🗄️ Esquema de base de datos verificado correctamente.")

    # ----------------------------------------------------------
    # Funciones básicas con reconexión y logging seguro
    # ----------------------------------------------------------
    async def exec(self, query: str, *params):
        try:
            async with self.pool.connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                await con.commit()
        except Exception as e:
            logging.error(f"❌ Error ejecutando query: {e}\nSQL: {query}")
            raise

    async def one(self, query: str, *params):
        try:
            async with self.pool.connection() as con:
                con.row_factory = dict_row
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchone()
        except Exception as e:
            logging.error(f"❌ Error en query (one): {e}\nSQL: {query}")
            raise

    async def all(self, query: str, *params):
        try:
            async with self.pool.connection() as con:
                con.row_factory = dict_row
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchall()
        except Exception as e:
            logging.error(f"❌ Error en query (all): {e}\nSQL: {query}")
            raise
