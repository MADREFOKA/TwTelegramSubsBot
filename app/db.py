import logging
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def init(self):
        """Inicializa el pool de conexiones"""
        self.pool = AsyncConnectionPool(
            conninfo=self.dsn,
            min_size=1,
            max_size=5,
            kwargs={"sslmode": "require"},
            open=True,
        )
        logging.info("✅ Pool de conexiones PostgreSQL inicializado.")

    async def exec(self, query: str, *params):
        for attempt in range(2):
            try:
                async with self.pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(query.replace("?", "%s"), params)
                        await conn.commit()
                        return
            except psycopg.OperationalError as e:
                logging.warning(f"⚠️ Reintentando exec por error operativo: {e}")
                await self.reconnect()
        logging.error("❌ Exec falló después de reintentar conexión.")

    async def one(self, query: str, *params):
        for attempt in range(2):
            try:
                async with self.pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(query.replace("?", "%s"), params)
                        row = await cur.fetchone()
                        return row
            except psycopg.OperationalError as e:
                logging.warning(f"⚠️ Reintentando one por error operativo: {e}")
                await self.reconnect()
        logging.error("❌ One falló después de reintentar conexión.")

    async def all(self, query: str, *params):
        for attempt in range(2):
            try:
                async with self.pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(query.replace("?", "%s"), params)
                        rows = await cur.fetchall()
                        return rows
            except psycopg.OperationalError as e:
                logging.warning(f"⚠️ Reintentando all por error operativo: {e}")
                await self.reconnect()
        logging.error("❌ All falló después de reintentar conexión.")

    async def reconnect(self):
        """Reconecta el pool cuando una conexión SSL muere."""
        try:
            if self.pool:
                await self.pool.close()
            self.pool = AsyncConnectionPool(
                conninfo=self.dsn,
                min_size=1,
                max_size=5,
                kwargs={"sslmode": "require"},
                open=True,
            )
            logging.info("🔄 Pool PostgreSQL reconectado correctamente.")
        except Exception as e:
            logging.error(f"❌ Error al reconectar pool: {e}")
