"""
Настройки, меняемые на ходу из дашборда.

Отдельный слой поверх `.env`, а не замена ему. `.env` задаёт, с чем бот
СТАРТУЕТ; здесь лежит то, что оператор меняет во время работы: включена ли
стратегия, каким процентом депозита она рискует, насколько близко ставит стоп.

Файл читается на каждом цикле, поэтому изменение вступает в силу без
перезапуска. Уже открытые позиции при этом не трогаются: их размер и стоп
посчитаны при входе, и менять их задним числом нельзя.

Все значения ограничиваются диапазоном при записи. Опечатка в поле «риск»
(5 вместо 0.5) — это не косметика, а десятикратный размер позиции, поэтому
проверка стоит на входе, а не на совести того, кто вводит.
"""

import json
import os
import threading

import config
from logger import log

SETTINGS_FILE = os.path.join(config.DATA_DIR, 'runtime_settings.json')
STRATEGIES = ('FIBO', 'SMC', 'LEVELS')

# Границы разумного. Верхняя граница риска намеренно невелика: 5% на сделку
# при винрейте около трети — это разорение на серии из десяти минусов.
LIMITS = {
    'risk_pct':     (0.05, 5.0),
    'min_stop_pct': (0.1, 20.0),
    'deposit':      (10.0, 10_000_000.0),
    'max_slots':    (1, 20),
}

_lock = threading.Lock()
_cache = None
_mtime = None


def _defaults():
    return {
        name: {
            'enabled': True,
            'risk_pct': float(config.RISK_PER_TRADE),
            'min_stop_pct': round(float(config.MIN_SL_PERCENT) * 100, 3),
            'deposit': float(config.PAPER_START_BALANCES.get(name,
                                                             config.PAPER_START_BALANCE)),
            'max_slots': int(config.SLOTS_PER_STRATEGY or config.MAX_ACTIVE_PAIRS),
        }
        for name in STRATEGIES
    }


def _clamp(field, value, fallback):
    low, high = LIMITS[field]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if value != value:                       # NaN
        return fallback
    value = min(max(value, low), high)
    return int(value) if field == 'max_slots' else value


def load(force=False):
    """
    Текущие настройки. Перечитывает файл, только если он изменился —
    вызывается каждый цикл и не должен превращаться в дисковую нагрузку.
    """
    global _cache, _mtime
    with _lock:
        try:
            stamp = os.path.getmtime(SETTINGS_FILE)
        except OSError:
            stamp = None

        if _cache is not None and not force and stamp == _mtime:
            return _cache

        data = _defaults()
        if stamp is not None:
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as fh:
                    stored = json.load(fh)
                for name in STRATEGIES:
                    item = (stored.get(name) or {})
                    data[name]['enabled'] = bool(item.get('enabled', True))
                    for field in ('risk_pct', 'min_stop_pct', 'deposit', 'max_slots'):
                        if field in item:
                            data[name][field] = _clamp(field, item[field],
                                                       data[name][field])
            except Exception as exc:
                log(f"⚠️ runtime_settings.json нечитаем ({exc}) — берём значения из .env")

        _cache, _mtime = data, stamp
        return data


def save(changes):
    """
    Применяет изменения и возвращает итоговые настройки.

    Принимает частичный набор: дашборд шлёт только то, что трогали.
    """
    global _cache, _mtime
    data = json.loads(json.dumps(load()))     # копия, чтобы не портить кэш

    for name in STRATEGIES:
        item = (changes.get(name) or {})
        if 'enabled' in item:
            data[name]['enabled'] = bool(item['enabled'])
        for field in ('risk_pct', 'min_stop_pct', 'deposit', 'max_slots'):
            if field in item:
                data[name][field] = _clamp(field, item[field], data[name][field])

    with _lock:
        try:
            tmp = SETTINGS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
        except Exception as exc:
            log(f"⚠️ Не удалось сохранить настройки: {exc}")
            return data
        _cache = data
        try:
            _mtime = os.path.getmtime(SETTINGS_FILE)
        except OSError:
            _mtime = None

    log('⚙️ Настройки изменены: ' + ', '.join(
        f"{name} {'вкл' if data[name]['enabled'] else 'ВЫКЛ'} "
        f"риск {data[name]['risk_pct']}% стоп>={data[name]['min_stop_pct']}%"
        for name in STRATEGIES))
    return data


# ── Точечные запросы ─────────────────────────────────────────────────────────

def enabled(strategy):
    return bool(load().get(strategy, {}).get('enabled', True))


def risk_pct(strategy):
    return float(load().get(strategy, {}).get('risk_pct', config.RISK_PER_TRADE))


def min_stop_pct(strategy):
    """Минимальная дистанция стопа, в ДОЛЯХ цены (а не в процентах)."""
    value = load().get(strategy, {}).get('min_stop_pct')
    if value is None:
        return float(config.MIN_SL_PERCENT)
    return float(value) / 100.0


def deposit(strategy):
    return float(load().get(strategy, {}).get('deposit',
                                              config.PAPER_START_BALANCE))


def max_slots(strategy):
    return int(load().get(strategy, {}).get(
        'max_slots', config.SLOTS_PER_STRATEGY or config.MAX_ACTIVE_PAIRS))
