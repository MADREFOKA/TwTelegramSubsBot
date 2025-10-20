# app/db.py
import os
import logging
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class Database:
    def __init__(self):
        self.url = os.getenv("DATABASE_URL")
        if not self.url:
            raise ValueError("❌ Falta DATABASE_URL")

        # Crea el pool sin abrir (como recomienda psycopg >=3.2)
        self.pool = AsyncConnectionPool(self.url, open=False)

    async def init(self):
        """Inicializa las tablas e índices si no existen y abre la pool."""
        try:
            await self.pool.open()  # 👈 Abrimos explícitamente la conexión
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

            CREATE TABLE IF NOT EXISTS audit_logs (
              id SERIAL PRIMARY KEY,
              broadcaster_id TEXT NOT NULL,
              group_id BIGINT NOT NULL,
              streamer_slug TEXT,
              etiqueta_semana TEXT,
              total_miembros INTEGER,
              expulsados INTEGER,
              sin_vinculo INTEGER,
              fallos INTEGER,
              fecha_auditoria TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_broadcaster_slug ON broadcasters(slug);
            CREATE INDEX IF NOT EXISTS idx_oauth_telegram ON oauth_states(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_audit_slug ON audit_logs(streamer_slug);
            """

            async with self.pool.connection() as con:
                async with con.cursor() as cur:
                    for stmt in [s.strip() for s in CREATE_SQL.split(";") if s.strip()]:
                        await cur.execute(stmt)
            logging.info("🗄️ Esquema de base de datos verificado correctamente.")
        except Exception as e:
            logging.error(f"❌ Error al inicializar la base de datos: {e}")

    async def exec(self, query: str, *params):
        """Ejecuta una query sin retorno."""
        try:
            async with self.pool.connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
        except Exception as e:
            logging.error(f"❌ Error ejecutando query: {e}\nSQL: {query}")

    async def one(self, query: str, *params):
        """Devuelve una fila o None."""
        try:
            async with self.pool.connection() as con:
                con.row_factory = dict_row
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchone()
        except Exception as e:
            logging.error(f"❌ Error en query (one): {e}\nSQL: {query}")
            return None

    async def all(self, query: str, *params):
        """Devuelve todas las filas."""
        try:
            async with self.pool.connection() as con:
                con.row_factory = dict_row
                async with con.cursor() as cur:
                    await cur.execute(query.replace("?", "%s"), params)
                    return await cur.fetchall()
        except Exception as e:
            logging.error(f"❌ Error en query (all): {e}\nSQL: {query}")
            return []
