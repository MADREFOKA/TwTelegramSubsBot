# app/security.py
import os
import base64
import secrets
from cryptography.fernet import Fernet

FERNET_KEY = os.environ.get("FERNET_KEY")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY missing in environment variables")

fernet = Fernet(FERNET_KEY)

def encrypt_token(s: str) -> str:
    return fernet.encrypt(s.encode()).decode()

def decrypt_token(s: str) -> str:
    return fernet.decrypt(s.encode()).decode()

def generate_dashboard_token() -> str:
    """Genera un token único para acceso al dashboard."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
