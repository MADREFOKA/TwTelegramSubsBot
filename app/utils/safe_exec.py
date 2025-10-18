# app/utils/safe_exec.py
import logging
import asyncio
import functools
import traceback
import psycopg

def safe_execute(retries: int = 2, delay: float = 1.5):
    """
    Decorador para capturar errores en comandos asíncronos.
    Reintenta N veces antes de abortar.
    Si la base de datos pierde conexión, reabre el pool automáticamente.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            self_obj = args[0] if args else None  # acceso a self.db si existe
            for attempt in range(1, retries + 1):
                try:
                    return await func(*args, **kwargs)
                except psycopg.OperationalError as e:
                    logging.error(f"⚠️ Error de conexión a la BD en {func.__name__}: {e}")
                    if self_obj and hasattr(self_obj, "db") and hasattr(self_obj.db, "pool"):
                        try:
                            logging.info("🔁 Reabriendo pool de conexión a NeonDB...")
                            await self_obj.db.pool.close()
                            await self_obj.db.pool.open()
                            logging.info("✅ Pool reabierto correctamente.")
                        except Exception as re_err:
                            logging.error(f"❌ Error al reabrir pool: {re_err}")
                    if attempt < retries:
                        await asyncio.sleep(delay)
                    else:
                        logging.error(f"❌ Fallo persistente en {func.__name__}: {e}")
                except Exception as e:
                    logging.error(f"❌ Error inesperado en {func.__name__}: {e}")
                    traceback.print_exc()
                    if attempt < retries:
                        logging.info(f"🔁 Reintentando {func.__name__} (intento {attempt+1}/{retries})...")
                        await asyncio.sleep(delay)
                    else:
                        logging.error(f"❌ {func.__name__} falló tras {retries} intentos.")
        return wrapper
    return decorator
