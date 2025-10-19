# app/security.py
import os
import logging
from cryptography.fernet import Fernet
from typing import Optional

class TokenCipher:
    def __init__(self):
        key = os.getenv("FERNET_KEY")
        if key:
            try:
                self.fernet: Optional[Fernet] = Fernet(key.encode())
                logging.info("🔐 Cifrado Fernet inicializado.")
            except Exception as e:
                logging.error(f"Error al inicializar Fernet: {e}")
                self.fernet = None
        else:
            logging.warning("⚠️ FERNET_KEY no definida, los tokens no se cifrarán.")
            self.fernet = None

    def encrypt(self, data: str) -> str:
        if not data:
            return data
        if not self.fernet:
            return data
        return self.fernet.encrypt(data.encode()).decode()

    def decrypt(self, data: str) -> str:
        if not data:
            return data
        if not self.fernet:
            return data
        try:
            return self.fernet.decrypt(data.encode()).decode()
        except Exception:
            return data
