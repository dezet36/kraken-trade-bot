"""
Как числа и имена выглядят в Telegram: цены, деньги, R, время, стратегии.

ОДНО МЕСТО НА ПАНЕЛЬ И УВЕДОМЛЕНИЯ. Позиция из сообщения о входе и та же
позиция в карточке панели обязаны читаться одинаково: если в одном месте
«−$56.20», а в другом «-56.2», человек видит две разные сделки.

Всё, что приходит извне (обоснования модели, причины, пары), проходит через
esc(): сообщения шлются с разметкой HTML, и «<» в тексте модели делал
сообщение нечитаемым для Telegram — оно просто не отправлялось.
"""

import html
from datetime import datetime, timezone

# Имена стратегий для человека. Внутренние (FIBO, LLM) остаются в данных и
# в кнопках — по ним панель находит стратегию.
NAMES = {'FIBO': 'Фибо', 'SMC': 'SMC', 'LEVELS': 'Уровни', 'RSIBB': 'Боллинджер', 'LLM': 'ИИ'}

# Порядок показа: как на сайте.
ORDER = ('FIBO', 'SMC', 'LEVELS', 'RSIBB', 'LLM')

MINUS = '−'
RULE = '━━━━━━━━━━━━━━━━━━━━'


def name(strategy):
    return NAMES.get(strategy, strategy or '—')


def ordered(names):
    """Стратегии в порядке показа; незнакомые — в конце по алфавиту."""
    names = list(names or ())
    known = [n for n in ORDER if n in names]
    return known + sorted(n for n in names if n not in ORDER)


def esc(text):
    return html.escape(str(text if text is not None else ''), quote=False)


def num(value, default=0.0):
    try:
        out = float(value)
        return out if out == out else default          # NaN
    except (TypeError, ValueError):
        return default


def price(value):
    """Цена с точностью по величине: BTC $80 000 и SHIB $0.000012 — одинаково читаемо."""
    p = num(value)
    a = abs(p)
    if a >= 1000:
        return f'{p:.2f}'
    if a >= 1:
        return f'{p:.4f}'
    if a >= 0.01:
        return f'{p:.6f}'
    if a >= 0.0001:
        return f'{p:.8f}'
    return f'{p:.10f}'


def _sign(value):
    return '+' if value > 0 else (MINUS if value < 0 else '')


def money(value, signed=True):
    """$ с копейками до тысячи, без копеек после: −$56.20, +$1 204, $50 672."""
    v = num(value)
    body = f'{abs(v):,.0f}' if abs(v) >= 1000 else f'{abs(v):,.2f}'
    body = body.replace(',', ' ')
    return f"{_sign(v) if signed else (MINUS if v < 0 else '')}${body}"


def r(value):
    v = num(value)
    return f'{_sign(v)}{abs(v):.2f}R'


def pct(value, signed=True, digits=2):
    v = num(value)
    return f"{_sign(v) if signed else (MINUS if v < 0 else '')}{abs(v):.{digits}f}%"


def duration(minutes):
    m = int(max(0, num(minutes)))
    days, rest = divmod(m, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f'{days}д {hours}ч'
    if hours:
        return f'{hours}ч {mins:02d}м'
    return f'{mins}м'


def coin(pair):
    """DOGEUSDT → DOGE: в списках пара короче, в карточке — полностью."""
    pair = str(pair or '')
    return pair[:-4] if pair.endswith('USDT') and len(pair) > 4 else pair


def side_icon(direction):
    return '🟢' if str(direction).upper() in ('LONG', 'BUY', 'BULLISH') else '🔴'


def result_icon(value):
    v = num(value)
    return '✅' if v > 0 else ('⚪' if v == 0 else '❌')


def mode_label(mode):
    """Режим счёта словами. До 26.09.2026 бумажный счёт подписывался «🔴 LIVE»."""
    mode = str(mode or '').upper()
    if mode == 'PAPER':
        return '📝 бумажный счёт'
    if mode == 'DEMO':
        return '🧪 демо-счёт'
    return '🔴 боевой счёт'


def parse_time(value):
    """ISO-строка или datetime → aware UTC; None, если не читается."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def minutes_since(value, now=None):
    dt = parse_time(value)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 60)


def clock(value):
    """26.09 08:05 UTC."""
    dt = parse_time(value)
    return dt.strftime('%d.%m %H:%M UTC') if dt else '—'


def cut(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'
