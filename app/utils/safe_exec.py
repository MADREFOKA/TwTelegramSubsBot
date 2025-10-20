# app/utils/safe_exec.py
import logging
import asyncio

def safe_exec(max_retries=2):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logging.error(f"❌ Error inesperado en {func.__name__}: {e}")
                    if attempt < max_retries - 1:
                        logging.info(f"🔁 Reintentando {func.__name__} (intento {attempt+2}/{max_retries})...")
                        await asyncio.sleep(1)
            logging.error(f"❌ {func.__name__} falló tras {max_retries} intentos.")
        return wrapper
    return decorator

