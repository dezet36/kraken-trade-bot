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
from datetime import datetime
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
    # Предел на ВЕСЬ портфель: сколько процентов депозита может стоять под
    # риском одновременно, считая все стратегии вместе. Ноль отключает.
    'portfolio_risk_pct':  (0.0, 100.0),
    # Максимум открытых позиций и ордеров суммарно. Ноль отключает.
    'portfolio_max_positions': (0, 60),
    # Дневной предел убытка в процентах от депозита. Ноль отключает.
    'daily_loss_pct': (0.0, 50.0),
}

# Общие настройки портфеля хранятся отдельным разделом: они не принадлежат
# ни одной стратегии, а ограничивают их все вместе. Без такого предела
# каждая стратегия соблюдает СВОЙ лимит слотов, и три стратегии по шесть
# позиций при риске 0.5% дают 9% депозита под риском одновременно — при том
# что ни одна из них своих правил не нарушила.
PORTFOLIO = 'PORTFOLIO'
PORTFOLIO_FIELDS = ('portfolio_risk_pct', 'portfolio_max_positions',
                    'daily_loss_pct')

_lock = threading.Lock()
_cache = None
_mtime = None


def _default_slots(strategy):
    """
    Слотов по умолчанию — из настроек САМОЙ стратегии, если они у неё есть.

    Иначе получается тихое расхождение: замер уровней считался на шести
    слотах, а бот брал общее число из конфига. Работать он будет, но
    торговать не то, что измерено, и заметить это можно только через
    месяцы по разошедшейся доходности.
    """
    if config.SLOTS_PER_STRATEGY:
        return int(config.SLOTS_PER_STRATEGY)
    if strategy == 'LEVELS':
        try:
            from levels import params as levels_params
            return int(levels_params.MAX_POSITIONS)
        except Exception:                          # noqa: BLE001
            pass
    return int(config.MAX_ACTIVE_PAIRS)


def _portfolio_defaults():
    """
    По умолчанию предел ВЫКЛЮЧЕН.

    Включённое по умолчанию ограничение, о котором оператор не знает, — это
    сделки, которые бот молча не открыл. Пусть лучше решение будет
    осознанным: панель показывает текущую загрузку, и включить предел можно
    одним полем.
    """
    return {
        'portfolio_risk_pct': float(os.getenv('PORTFOLIO_RISK_PCT', 0) or 0),
        'portfolio_max_positions': int(os.getenv('PORTFOLIO_MAX_POSITIONS', 0) or 0),
        'daily_loss_pct': float(os.getenv('DAILY_LOSS_PCT', 0) or 0),
    }


def _defaults():
    base = {
        name: {
            'enabled': True,
            'risk_pct': float(config.RISK_PER_TRADE),
            'min_stop_pct': round(float(config.MIN_SL_PERCENT) * 100, 3),
            'deposit': float(config.PAPER_START_BALANCES.get(name,
                                                             config.PAPER_START_BALANCE)),
            'max_slots': _default_slots(name),
        }
        for name in STRATEGIES
    }
    base[PORTFOLIO] = _portfolio_defaults()
    return base


def _clamp(field, value, fallback):
    low, high = LIMITS[field]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if value != value:                       # NaN
        return fallback
    value = min(max(value, low), high)
    return (int(value) if field in ('max_slots', 'portfolio_max_positions')
            else value)


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
                stored_portfolio = stored.get(PORTFOLIO) or {}
                for field in PORTFOLIO_FIELDS:
                    if field in stored_portfolio:
                        data[PORTFOLIO][field] = _clamp(
                            field, stored_portfolio[field], data[PORTFOLIO][field])
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
    before = json.loads(json.dumps(load()))   # снимок ДО правки — для истории
    data = json.loads(json.dumps(load()))     # копия, чтобы не портить кэш

    portfolio = changes.get(PORTFOLIO) or {}
    for field in PORTFOLIO_FIELDS:
        if field in portfolio:
            data[PORTFOLIO][field] = _clamp(field, portfolio[field],
                                            data[PORTFOLIO][field])

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
    _write_history(before, data)
    return data


HISTORY_FILE = os.path.join(config.DATA_DIR, 'settings_history.jsonl')
HISTORY_MAX = 500


def _flatten(data):
    """Настройки одним словарём «раздел.поле -> значение» — так их легко сравнить."""
    flat = {}
    for section, values in (data or {}).items():
        if isinstance(values, dict):
            for field, value in values.items():
                flat[f'{section}.{field}'] = value
    return flat


def _write_history(before, after):
    """
    Запоминает, что именно изменилось и когда.

    Зачем отдельно от общего журнала: там сотни строк в час, и найти в них
    «когда я поднял риск» невозможно. А вопрос этот возникает каждый раз,
    когда результаты меняются: сначала надо понять, менялось ли что-то в
    настройках, и только потом искать причину в рынке.

    Пишутся ТОЛЬКО отличия. Запись «ничего не изменилось» — это шум, а
    дашборд шлёт полный набор полей при каждом нажатии «Применить».
    """
    old, new = _flatten(before), _flatten(after)
    changes = [{'field': key, 'from': old.get(key), 'to': new[key]}
               for key in new if old.get(key) != new[key]]
    if not changes:
        return
    record = {'at': datetime.now().isoformat(timespec='seconds'), 'changes': changes}
    try:
        with open(HISTORY_FILE, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError as exc:
        log(f"⚠️ не удалось записать историю настроек: {exc}")


def history(limit=100):
    """Последние изменения настроек, новые сверху."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, encoding='utf-8') as fh:
            lines = fh.readlines()[-HISTORY_MAX:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


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


def portfolio_risk_pct():
    """Предел риска на весь портфель в процентах. 0 — выключен."""
    return float(load().get(PORTFOLIO, {}).get('portfolio_risk_pct', 0) or 0)


def portfolio_max_positions():
    """Предел числа позиций и ордеров на весь портфель. 0 — выключен."""
    return int(load().get(PORTFOLIO, {}).get('portfolio_max_positions', 0) or 0)


def daily_loss_pct():
    """
    Дневной предел убытка в процентах от депозита. 0 — выключен.

    Отдельно от предела портфеля: тот ограничивает риск, стоящий в рынке
    ОДНОВРЕМЕННО, и молчит, когда десять сделок подряд закрылись в минус по
    очереди. Плохой день так и выглядит: каждая сделка по правилам, а к
    вечеру депозита нет.
    """
    return float(load().get(PORTFOLIO, {}).get('daily_loss_pct', 0) or 0)


def max_slots(strategy):
    return int(load().get(strategy, {}).get(
        'max_slots', config.SLOTS_PER_STRATEGY or config.MAX_ACTIVE_PAIRS))
