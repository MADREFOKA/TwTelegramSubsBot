# app/utils/safe_exec.py
import logging
import asyncio
import functools
import traceback

def safe_execute(retries: int = 2, delay: float = 1.0):
    """
    Decorador para capturar y registrar errores en comandos asíncronos.
    Reintenta N veces antes de abortar (por fallos de red o BD temporales).
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logging.error(f"⚠️ Error en {func.__name__}: {e}")
                    traceback.print_exc()
                    if attempt < retries:
                        logging.info(f"🔁 Reintentando {func.__name__} (intento {attempt+1}/{retries})...")
                        await asyncio.sleep(delay)
                    else:
                        logging.error(f"❌ {func.__name__} falló tras {retries} intentos.")
            # No lanza excepción, evita que el bot se caiga
        return wrapper
    return decorator
