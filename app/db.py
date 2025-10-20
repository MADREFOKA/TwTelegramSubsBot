# app/db.py
import logging
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool: AsyncConnectionPool | None = None

    async def init(self):
        """Crea el pool de conexión y abre la base de datos."""
        try:
            self.pool = AsyncConnectionPool(self.dsn, kwargs={"autocommit": True, "row_factory": dict_row})
            await self.pool.open()
            logging.info("🗄️ Esquema de base de datos verificado correctamente.")
        except Exception as e:
            logging.error(f"❌ Error abriendo conexión con BD: {e}")
            raise

    async def _ensure_pool(self):
        """Reabre el pool si está cerrado o en mal estado."""
        if not self.pool or self.pool.closed:
            logging.warning("🔁 Reabriendo pool de conexión a NeonDB...")
            self.pool = AsyncConnectionPool(self.dsn, kwargs={"autocommit": True, "row_factory": dict_row})
            await self.pool.open()

    async def exec(self, query: str, *params):
        """Ejecuta un INSERT/UPDATE/DELETE con reintento."""
        await self._ensure_pool()
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
        except psycopg.OperationalError as e:
            logging.warning(f"⚠️ Reintentando exec por error operativo: {e}")
            await self._ensure_pool()
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
        except Exception as e:
            logging.error(f"❌ Error ejecutando query: {e}\nSQL: {query}")
            raise

    async def one(self, query: str, *params):
        """Devuelve una sola fila."""
        await self._ensure_pool()
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchone()
        except psycopg.OperationalError as e:
            logging.warning(f"⚠️ Reintentando one por error operativo: {e}")
            await self._ensure_pool()
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchone()
        except Exception as e:
            logging.error(f"❌ Error en query (one): {e}\nSQL: {query}")
            raise

    async def all(self, query: str, *params):
        """Devuelve todas las filas."""
        await self._ensure_pool()
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchall()
        except psycopg.OperationalError as e:
            logging.warning(f"⚠️ Reintentando all por error operativo: {e}")
            await self._ensure_pool()
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchall()
        except Exception as e:
            logging.error(f"❌ Error en query (all): {e}\nSQL: {query}")
            raise
