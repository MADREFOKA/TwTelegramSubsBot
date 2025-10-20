# app/security.py
import os
from typing import Optional

try:
    from cryptography.fernet import Fernet  # opcional
except Exception:
    Fernet = None

class TokenCipher:
    """Cifra/descifra strings si hay FERNET_KEY. Si no, hace passthrough."""
    def __init__(self) -> None:
        key = os.getenv("FERNET_KEY")
        if key and Fernet:
            self.fernet: Optional[Fernet] = Fernet(key.encode())
        else:
            self.fernet = None

    def encrypt(self, text: str | None) -> str | None:
        if not text:
            return text
        if not self.fernet:
            return text
        return self.fernet.encrypt(text.encode()).decode()

    def decrypt(self, text: str | None) -> str | None:
        if not text:
            return text
        if not self.fernet:
            return text
        return self.fernet.decrypt(text.encode()).decode()
