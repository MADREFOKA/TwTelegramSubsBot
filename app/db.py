# app/db.py
import logging
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: AsyncConnectionPool | None = None

    async def init(self):
        """Inicializa el pool de conexiones."""
        self.pool = AsyncConnectionPool(
            conninfo=self.dsn,
            min_size=1,
            max_size=5,
            kwargs={"sslmode": "require"},
            open=True,
        )
        logging.info("✅ Pool de conexiones PostgreSQL inicializado.")

    async def close(self):
        """Cierra el pool (útil para shutdown limpio)."""
        try:
            if self.pool:
                await self.pool.close()
                self.pool = None
        except Exception as e:
            logging.warning(f"⚠️ Error cerrando pool: {e}")

    # ───────────────────────────────────────────────
    # Helpers internos
    # ───────────────────────────────────────────────
    def _normalize_params(self, params):
        # Fuerza conversión de booleanos Python → SQL (consistente en exec/one/all)
        return tuple(True if p is True else False if p is False else p for p in params)

    def _normalize_query(self, query: str) -> str:
        # Compat con queries antiguas que usen "?"
        # OJO: si tienes "?" dentro de textos/JSON, esto puede ser peligroso.
        return query.replace("?", "%s")

    # ───────────────────────────────────────────────
    # Core DB API
    # ───────────────────────────────────────────────
    async def exec(self, query: str, *params):
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

        q = self._normalize_query(query)
        p = self._normalize_params(params)

        last_err = None
        for _ in range(2):
            try:
                async with self.pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(q, p)
                    await conn.commit()
                return
            except psycopg.OperationalError as e:
                last_err = e
                logging.warning(f"⚠️ Reintentando exec por error operativo: {e}")
                await self.reconnect()
            except Exception:
                logging.exception("❌ Exec falló (no operativo):")
                raise

        logging.error("❌ Exec falló después de reintentar conexión.")
        raise last_err or RuntimeError("DB exec failed")

    async def one(self, query: str, *params):
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

        q = self._normalize_query(query)
        p = self._normalize_params(params)

        last_err = None
        for _ in range(2):
            try:
                async with self.pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(q, p)
                        return await cur.fetchone()
            except psycopg.OperationalError as e:
                last_err = e
                logging.warning(f"⚠️ Reintentando one por error operativo: {e}")
                await self.reconnect()
            except Exception:
                logging.exception("❌ One falló (no operativo):")
                raise

        logging.error("❌ One falló después de reintentar conexión.")
        raise last_err or RuntimeError("DB one failed")

    async def all(self, query: str, *params):
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

        q = self._normalize_query(query)
        p = self._normalize_params(params)

        last_err = None
        for _ in range(2):
            try:
                async with self.pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(q, p)
                        return await cur.fetchall()
            except psycopg.OperationalError as e:
                last_err = e
                logging.warning(f"⚠️ Reintentando all por error operativo: {e}")
                await self.reconnect()
            except Exception:
                logging.exception("❌ All falló (no operativo):")
                raise

        logging.error("❌ All falló después de reintentar conexión.")
        raise last_err or RuntimeError("DB all failed")

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
            raise

    # ============================================================
    # 👥 GROUP_MEMBERS — Guardar suscriptores
    # ============================================================
    async def add_group_member(
        self,
        group_id: int,
        streamer_slug: str,
        broadcaster_id: str,
        telegram_id: int,
        username: str = None,
        twitch_id: str = None,
    ):
        """
        Guarda o actualiza un usuario en la tabla group_members.
        Se llama cuando el usuario se vincula y es sub del streamer.
        """
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO group_members (
                            group_id, streamer_slug, broadcaster_id, telegram_id,
                            username, twitch_id, is_subscribed, joined_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW())
                        ON CONFLICT (group_id, telegram_id)
                        DO UPDATE SET
                            streamer_slug=EXCLUDED.streamer_slug,
                            broadcaster_id=EXCLUDED.broadcaster_id,
                            username=EXCLUDED.username,
                            twitch_id=EXCLUDED.twitch_id,
                            is_subscribed=TRUE,
                            joined_at=NOW();
                    """, (group_id, streamer_slug, broadcaster_id, telegram_id, username, twitch_id))
                await conn.commit()
            logging.info(f"✅ Guardado en group_members: {username} ({telegram_id}) → {streamer_slug}")
        except Exception as e:
            logging.error(f"❌ Error al guardar miembro en group_members: {e}")
            raise

    # ============================================================
    # 👑 PRIVILEGED_USERS — Añadir / Eliminar / Listar / Obtener
    # ============================================================
    async def add_privileged_user(
        self,
        streamer_slug: str,
        broadcaster_id: str,
        telegram_id: int,
        username: str = None,
        added_by: int = None,
    ):
        """
        Añade un usuario privilegiado (protegido en auditorías).
        Se usa en /priv_add.
        """
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

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
            raise

    async def remove_privileged_user(self, streamer_slug: str, broadcaster_id: str, telegram_id: int):
        """
        Elimina un usuario privilegiado del streamer.
        Se usa en /priv_delete o desde el dashboard.
        """
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

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
            raise

    async def list_privileged_users(self, streamer_slug: str):
        """
        Devuelve todos los usuarios privilegiados de un streamer (para dashboard o /priv_list).
        """
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

        try:
            async with self.pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("""
                        SELECT telegram_id, username, added_at
                        FROM privileged_users
                        WHERE streamer_slug=%s
                        ORDER BY added_at DESC
                    """, (streamer_slug,))
                    return await cur.fetchall()
        except Exception as e:
            logging.error(f"❌ Error al listar usuarios privilegiados: {e}")
            return []

    async def get_privileged_ids(self, streamer_slug: str) -> list[int]:
        """
        Obtiene todos los telegram_id de los usuarios privilegiados del streamer.
        Usado en auditorías para no expulsarlos.
        """
        if not self.pool:
            raise RuntimeError("DB pool no inicializado. Llama a await db.init()")

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
