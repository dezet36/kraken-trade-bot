"""
Журнал копировщика: строка в файл и на экран. Ключи и токены вычищаются.

Ключ Upscale (usk_…) и токен Telegram не должны попасть в журнал ни в
каком виде: журнал читают при разборе, копируют, пересылают.
"""

import os
import re
import sys
import threading
from datetime import datetime, timezone

from copier import config

_lock = threading.Lock()
_KEY_RE = re.compile(r'usk_[A-Za-z0-9_\-]+')
_BEARER_RE = re.compile(r'(Bearer\s+)\S+', re.IGNORECASE)
MAX_BYTES = 5_000_000


def scrub(text):
    """Текст без ключей Upscale, Bearer-заголовков и токена Telegram."""
    text = str(text)
    text = _KEY_RE.sub('usk_<KEY>', text)
    text = _BEARER_RE.sub(r'\1<KEY>', text)
    token = config.TG_TOKEN
    if token:
        text = text.replace(token, '<TOKEN>')
    return text


def log(message):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} {scrub(message)}"
    with _lock:
        try:
            print(line, flush=True)
        except Exception:                              # noqa: BLE001
            pass
        try:
            os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
            if os.path.exists(config.LOG_PATH) and os.path.getsize(config.LOG_PATH) > MAX_BYTES:
                os.replace(config.LOG_PATH, config.LOG_PATH + '.1')
            with open(config.LOG_PATH, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except Exception as exc:                       # noqa: BLE001
            sys.stderr.write(f'журнал не записан: {exc}\n')
