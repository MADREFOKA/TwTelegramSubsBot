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
            
    # ============================================================
    # 👥 GROUP_MEMBERS — Guardar suscriptores
    # ============================================================
    async def add_group_member(self, group_id: int, streamer_slug: str, broadcaster_id: str,
                               telegram_id: int, username: str = None, twitch_login: str = None):
        """
        Guarda o actualiza un usuario en la tabla group_members.
        Se llama cuando el usuario se vincula y es sub del streamer.
        """
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO group_members (
                            group_id, streamer_slug, broadcaster_id, telegram_id, username, twitch_login,
                            is_subscribed, joined_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW())
                        ON CONFLICT (group_id, telegram_id)
                        DO UPDATE SET is_subscribed = TRUE, joined_at = NOW();
                    """, (group_id, streamer_slug, broadcaster_id, telegram_id, username, twitch_login))
                    await conn.commit()
                    logging.info(f"✅ Guardado en group_members: {username} ({telegram_id}) → {streamer_slug}")
        except Exception as e:
            logging.error(f"❌ Error al guardar miembro en group_members: {e}")


    # ============================================================
    # 👑 PRIVILEGED_USERS — Añadir / Eliminar / Listar / Obtener
    # ============================================================

    async def add_privileged_user(self, streamer_slug: str, broadcaster_id: str,
                                  telegram_id: int, username: str = None, added_by: int = None):
        """
        Añade un usuario privilegiado (protegido en auditorías).
        Se usa en /priv_add.
        """
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO privileged_users (
                            streamer_slug, broadcaster_id, telegram_id, username, added_by, added_at
                        )
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (broadcaster_id, telegram_id) DO NOTHING;
                    """, (streamer_slug, broadcaster_id, telegram_id, username, added_by))
                    await conn.commit()
                    logging.info(f"✅ Usuario privilegiado añadido: {username} ({telegram_id})")
        except Exception as e:
            logging.error(f"❌ Error al añadir usuario privilegiado: {e}")


    async def remove_privileged_user(self, streamer_slug: str, broadcaster_id: str, telegram_id: int):
        """
        Elimina un usuario privilegiado del streamer.
        Se usa en /priv_delete o desde el dashboard.
        """
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        DELETE FROM privileged_users
                        WHERE streamer_slug=%s AND broadcaster_id=%s AND telegram_id=%s
                    """, (streamer_slug, broadcaster_id, telegram_id))
                    await conn.commit()
                    logging.info(f"🗑️ Usuario privilegiado eliminado: {telegram_id}")
        except Exception as e:
            logging.error(f"❌ Error al eliminar usuario privilegiado: {e}")

    async def list_privileged_users(self, streamer_slug: str):
        """
        Devuelve todos los usuarios privilegiados de un streamer (para dashboard o /priv_list).
        """
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("""
                        SELECT telegram_id, username, added_at
                        FROM privileged_users
                        WHERE streamer_slug=%s
                        ORDER BY added_at DESC
                    """, (streamer_slug,))
                    rows = await cur.fetchall()
                    return rows
        except Exception as e:
            logging.error(f"❌ Error al listar usuarios privilegiados: {e}")
            return []

    async def get_privileged_ids(self, streamer_slug: str) -> list[int]:
        """
        Obtiene todos los telegram_id de los usuarios privilegiados del streamer.
        Usado en auditorías para no expulsarlos.
        """
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT telegram_id FROM privileged_users WHERE streamer_slug=%s
                    """, (streamer_slug,))
                    rows = await cur.fetchall()
                    return [r[0] for r in rows] if rows else []
        except Exception as e:
            logging.error(f"❌ Error al obtener privileged_ids: {e}")
            return []
