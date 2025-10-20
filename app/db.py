import os
import logging
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv

load_dotenv()

class Config:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.base_url = os.getenv("BASE_URL")
        self.client_id = os.getenv("OAUTH_CLIENT_ID")
        self.client_secret = os.getenv("OAUTH_CLIENT_SECRET")
        self.bot_username = os.getenv("BOT_USERNAME")
        self.db_url = os.getenv("DATABASE_URL")
        self.port = int(os.getenv("PORT", "8080"))

class Database:
    def __init__(self):
        self.cfg = Config()
        self.pool = AsyncConnectionPool(
            conninfo=self.cfg.db_url,
            open=False,
            max_size=10,
            kwargs={"autocommit": True}
        )

    async def open(self):
        await self.pool.open()
        logging.info("🗄️ Conexión a base de datos abierta.")

    async def close(self):
        await self.pool.close()
        logging.info("🔒 Conexión a base de datos cerrada.")

    async def one(self, query, *params):
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query.replace("?", "%s"), params)
                return await cur.fetchone()

    async def all(self, query, *params):
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query.replace("?", "%s"), params)
                return await cur.fetchall()

    async def exec(self, query, *params):
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query.replace("?", "%s"), params)
