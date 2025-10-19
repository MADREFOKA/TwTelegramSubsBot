# app/utils/safe_exec.py
import logging
import asyncio

def safe_execute(retries=2, delay=1.5):
    """Decorador para manejar errores con reintentos y logs"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for attempt in range(1, retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logging.error(f"❌ Error inesperado en {func.__name__}: {e}")
                    if attempt < retries:
                        logging.info(f"🔁 Reintentando {func.__name__} (intento {attempt}/{retries})...")
                        await asyncio.sleep(delay)
                    else:
                        logging.error(f"❌ {func.__name__} falló tras {retries} intentos.")
                        return None
        return wrapper
    return decorator
