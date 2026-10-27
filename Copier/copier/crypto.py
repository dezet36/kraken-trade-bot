"""
Ключи участников в покое — Fernet (AES-128 + HMAC), мастер-ключ COPIER_SECRET_KEY.

Образец — crypto_security.py удалённой многопользовательской платформы
(коммит e11c7cd). Потеря мастер-ключа = все ключи нечитаемы: участникам
придётся подключиться заново. Мастер-ключ живёт только в .env копировщика
на сервере (600), в резервную копию базы не входит.

Сгенерировать: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from copier import config


class SecretKeyMissing(RuntimeError):
    pass


def _cipher(secret=None):
    key = secret if secret is not None else config.SECRET_KEY
    if not key:
        raise SecretKeyMissing('COPIER_SECRET_KEY не задан')
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:                           # noqa: BLE001
        raise SecretKeyMissing(f'COPIER_SECRET_KEY некорректен: {exc}') from None


def encrypt(plaintext, secret=None):
    if not plaintext:
        return ''
    return _cipher(secret).encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt(token, secret=None):
    if not token:
        return ''
    try:
        return _cipher(secret).decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken:
        raise SecretKeyMissing('ключ не расшифрован — сменился COPIER_SECRET_KEY?') from None


def fingerprint(api_key):
    """Чем показать ключ человеку и узнать повтор: хвост и короткий хэш, сам ключ — никогда."""
    api_key = api_key or ''
    digest = hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:8]
    return f'…{api_key[-4:]}#{digest}' if len(api_key) >= 8 else f'#{digest}'


def generate_master_key():
    return Fernet.generate_key().decode('utf-8')
