# app/db.py
import logging
import asyncio
from psycopg_pool import AsyncConnectionPool

class Database:
    def __init__(self):
        import os
        self.dsn = os.getenv("DATABASE_URL")
        self.pool = AsyncConnectionPool(self.dsn, open=False)
        self.lock = asyncio.Lock()

    async def open(self):
        if not self.pool.is_open:
            await self.pool.open()
            logging.info("🗄️ Pool de base de datos abierto correctamente.")

    async def exec(self, query, *params):
        await self.open()
        async with self.lock, self.pool.connection() as con:
            try:
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    await con.commit()
            except Exception as e:
                logging.error(f"❌ Error ejecutando query: {e}\nSQL: {query}")
                raise

    async def one(self, query, *params):
        await self.open()
        async with self.lock, self.pool.connection() as con:
            try:
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    row = await cur.fetchone()
                    if not row:
                        return None
                    colnames = [desc[0] for desc in cur.description]
                    return dict(zip(colnames, row))
            except Exception as e:
                logging.error(f"❌ Error en query (one): {e}\nSQL: {query}")
                return None

    async def all(self, query, *params):
        await self.open()
        async with self.lock, self.pool.connection() as con:
            try:
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    rows = await cur.fetchall()
                    colnames = [desc[0] for desc in cur.description]
                    return [dict(zip(colnames, r)) for r in rows]
            except Exception as e:
                logging.error(f"❌ Error en query (all): {e}\nSQL: {query}")
                return []
