"""
Шифрование API-ключей пользователей в покое (Fernet, симметричное AES-128).

Мастер-ключ берётся из env PLATFORM_SECRET_KEY (Fernet-ключ, base64, 32 байта).
Сгенерировать новый: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

ВАЖНО: при потере/смене PLATFORM_SECRET_KEY все сохранённые ключи становятся
нечитаемыми — пользователям придётся переподключить биржу.
"""

import os
from cryptography.fernet import Fernet, InvalidToken


class SecretKeyMissing(RuntimeError):
    pass


def _get_cipher() -> Fernet:
    key = os.getenv('PLATFORM_SECRET_KEY')
    if not key:
        raise SecretKeyMissing(
            "PLATFORM_SECRET_KEY не задан в окружении. Сгенерируй: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        raise SecretKeyMissing(f"PLATFORM_SECRET_KEY некорректен (нужен Fernet-ключ): {e}")


def encrypt(plaintext: str) -> str:
    """Шифрует строку (API-ключ/секрет). Возвращает base64-токен для хранения в БД."""
    if not plaintext:
        return ''
    cipher = _get_cipher()
    return cipher.encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt(token: str) -> str:
    """Расшифровывает токен из БД обратно в строку. Пустой токен -> пустая строка."""
    if not token:
        return ''
    cipher = _get_cipher()
    try:
        return cipher.decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken:
        raise InvalidToken(
            "Не удалось расшифровать ключ — вероятно, сменился PLATFORM_SECRET_KEY."
        )


def is_encrypted(token: str) -> bool:
    """Проверка, что строка — валидный Fernet-токен (для миграций/валидации)."""
    if not token:
        return False
    try:
        _get_cipher().decrypt(token.encode('utf-8'))
        return True
    except Exception:
        return False


def generate_master_key() -> str:
    """Утилита: сгенерировать новый мастер-ключ для PLATFORM_SECRET_KEY."""
    return Fernet.generate_key().decode('utf-8')
