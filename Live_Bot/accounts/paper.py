"""
Тестовые счета стратегий — по счёту на стратегию (решение владельца 08.10.2026;
docs/Архитектура_модули_2026-10-08.md, модуль 4, этап 2).

СЧЁТ РЕШАЕТ ДЕНЬГИ, СТРАТЕГИЯ — СЕТАП. Стратегия отдаёт сетап без денег
(strategies/contract.py): пару, сторону, вход, стоп, цели, сопровождение. Счёт
решает, берёт ли он сетап и на каких деньгах (decide):
  - допущена ли стратегия (enabled) и в какую сторону (sides);
  - риск на сделку, % депозита — на тесте у всех 1% (CLAUDE.md, «Риск»);
  - сколько позиций держать всего (max_slots) и в одну сторону
    (max_same_direction; не задан — с каким пределом стратегия измерена);
  - пределы портфеля: риск всех стратегий вместе, число позиций, убыток дня.
Исполнитель (бумажный брокер, боевой исполнитель) считает объём по этим
числам от цены заполнения и текущего депозита.

ХРАНЕНИЕ. accounts.json в каталоге данных — отдельно от настроек стратегий
(runtime_settings.json). Панель и Telegram меняют счета через settings_store:
он делит правку между владельцами полей и пишет одну запись в историю настроек
(settings_history.jsonl). Файл читается на каждом цикле, если изменился:
правка действует без перезапуска. Уже открытые позиции не трогаются — их
размер и стоп посчитаны при входе.

ПЕРЕНОС. До 08.10.2026 деньги лежали в runtime_settings.json в разделах
стратегий. Если accounts.json нет, а там они есть, значения переносятся при
первом чтении: копия прежнего файла — runtime_settings.before_accounts.json, в
истории настроек — запись «Счета · перенос». Прежние денежные поля в
runtime_settings.json после этого не читаются; панель снимает их при
следующей записи настроек.

БОЕВОЙ ПУТЬ. До подключения реальных счетов (этап 7) боевой цикл берёт деньги
из этих же счетов стратегий — как раньше брал из настроек стратегий.
"""

import json
import os
import shutil
import threading
from datetime import datetime

from logger import log
from strategies import registry

FILE_NAME = 'accounts.json'
LEGACY_NAME = 'runtime_settings.json'
BACKUP_NAME = 'runtime_settings.before_accounts.json'
HISTORY_NAME = 'settings_history.jsonl'

FIELDS = ('enabled', 'deposit', 'risk_pct', 'sides', 'max_slots', 'max_same_direction')

# Общие пределы портфеля — отдельным разделом: они не принадлежат ни одной
# стратегии, а ограничивают их все вместе. Без такого предела каждая стратегия
# соблюдает СВОЙ лимит слотов, и три стратегии по шесть позиций при риске 0.5%
# дают 9% депозита под риском одновременно — при том что ни одна из них своих
# правил не нарушила.
PORTFOLIO = 'PORTFOLIO'
PORTFOLIO_FIELDS = ('portfolio_risk_pct', 'portfolio_max_positions', 'daily_loss_pct')

# Ноль в поле «одновременных позиций» означает «без предела». Ноль выбран
# потому, что так же устроены пределы портфеля и дневного убытка: одно правило
# «ноль = выключено» на все ограничители, а не три разных.
UNLIMITED = 0

SIDES = ('both', 'long', 'short')

# Границы разумного. Опечатка в поле «риск» (5 вместо 0.5) — это не косметика,
# а десятикратный размер позиции, поэтому проверка стоит на входе. Верхняя
# граница риска намеренно невелика: 5% на сделку при винрейте около трети —
# разорение на серии из десяти минусов.
LIMITS = {
    'risk_pct':     (0.05, 5.0),
    'deposit':      (10.0, 10_000_000.0),
    # Одновременные позиции стратегии. НОЛЬ — без предела: стратегия берёт
    # столько сетапов, сколько нашла (см. _default_slots).
    'max_slots':    (0, 60),
    # Позиции стратегии в одну сторону. Ноль — без предела.
    'max_same_direction': (0, 60),
    # Сколько процентов депозита может стоять под риском одновременно, считая
    # все стратегии вместе. Ноль отключает.
    'portfolio_risk_pct':  (0.0, 100.0),
    # Максимум открытых позиций и ордеров суммарно. Ноль отключает.
    'portfolio_max_positions': (0, 60),
    # Дневной предел убытка в процентах от депозита. Ноль отключает.
    'daily_loss_pct': (0.0, 50.0),
}
_WHOLE = ('max_slots', 'max_same_direction', 'portfolio_max_positions')

_lock = threading.RLock()
_cache = {'key': None, 'data': None}


# ── Где лежит ───────────────────────────────────────────────────────────────

def _data_dir():
    # config — заново при каждом вызове: тесты перезагружают его, и каталог
    # данных, схваченный при импорте, был бы чужим (боевым).
    import config
    return config.DATA_DIR


def path():
    return os.path.join(_data_dir(), FILE_NAME)


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


# ── Умолчания и проверка полей ──────────────────────────────────────────────

def _default_slots():
    """
    По умолчанию предела НЕТ: стратегия берёт столько сетапов, сколько нашла.

    Раньше здесь стояло 5 — число, подобранное в июле 2026 под пул из 16 пар.
    Пул вырос, стратегий стало больше, и каждая ищет во всём пуле: предел
    выбрасывал сетапы не потому, что они плохи, а потому что не было слота. На
    бумажном тестировании это прямая потеря наблюдений.

    Чем за это платится: одновременный риск не ограничен сверху, а криптопары в
    проливе ходят вместе. Поэтому рядом — пределы на ВЕСЬ портфель и дневной
    предел убытка (выключены по умолчанию, включаются одним полем на панели).
    Осознанный выбор оператора сильнее: SLOTS_PER_STRATEGY в .env, а поле на
    панели перекрывает и его.
    """
    import config
    if config.SLOTS_PER_STRATEGY:
        return int(config.SLOTS_PER_STRATEGY)
    return UNLIMITED


def _portfolio_defaults():
    """
    По умолчанию пределы портфеля ВЫКЛЮЧЕНЫ: включённое по умолчанию
    ограничение, о котором оператор не знает, — это сделки, которые бот молча
    не открыл. Панель показывает загрузку, включить предел — одно поле.
    """
    return {
        'portfolio_risk_pct': float(os.getenv('PORTFOLIO_RISK_PCT', 0) or 0),
        'portfolio_max_positions': int(os.getenv('PORTFOLIO_MAX_POSITIONS', 0) or 0),
        'daily_loss_pct': float(os.getenv('DAILY_LOSS_PCT', 0) or 0),
    }


def _defaults():
    import config
    out = {code: {
        'enabled': True,
        'deposit': float(config.PAPER_START_BALANCES.get(code, config.PAPER_START_BALANCE)),
        'risk_pct': float(config.RISK_PER_TRADE),
        # Обе стороны — так бот вёл себя всегда. Выключить сторону молча
        # нельзя: это сделки, которых человек не досчитается, не понимая почему.
        'sides': 'both',
        'max_slots': _default_slots(),
        # None — как в замере стратегии (strategy_profile.max_same_direction).
        'max_same_direction': None,
    } for code in registry.codes()}
    out[PORTFOLIO] = _portfolio_defaults()
    return out


def _clamp(field, value, fallback):
    low, high = LIMITS[field]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if value != value:                       # NaN
        return fallback
    value = min(max(value, low), high)
    return int(value) if field in _WHOLE else value


def clean_sides(value, fallback='both'):
    """
    Разрешённые стороны — только из известного списка. Незнакомое значение НЕ
    считается «выключить всё»: опечатка в файле не должна тихо остановить
    торговлю — возвращается прежнее.
    """
    value = str(value or '').strip().lower()
    return value if value in SIDES else fallback


def _apply(data, changes):
    """Записанные значения (или правка) поверх data — с проверкой каждого поля.
    Чужие поля (настройки стратегий, уведомления) здесь не читаются."""
    changes = changes if isinstance(changes, dict) else {}
    portfolio = changes.get(PORTFOLIO)
    if isinstance(portfolio, dict):
        for field in PORTFOLIO_FIELDS:
            if field in portfolio:
                data[PORTFOLIO][field] = _clamp(field, portfolio[field], data[PORTFOLIO][field])
    for code in registry.codes():
        item = changes.get(code)
        if not isinstance(item, dict):
            continue
        account = data[code]
        if 'enabled' in item:
            account['enabled'] = bool(item['enabled'])
        for field in ('risk_pct', 'deposit', 'max_slots'):
            if field in item:
                account[field] = _clamp(field, item[field], account[field])
        if 'max_same_direction' in item:
            value = item['max_same_direction']
            account['max_same_direction'] = (
                None if value is None or value == ''
                else _clamp('max_same_direction', value, account['max_same_direction']))
        if 'sides' in item:
            account['sides'] = clean_sides(item['sides'], account['sides'])
    return data


# ── Чтение, запись, перенос ─────────────────────────────────────────────────

def load(force=False):
    """
    Счета стратегий и пределы портфеля. Файл перечитывается, только если
    изменился: зовётся каждый цикл и не должен становиться дисковой нагрузкой.
    Битый файл не останавливает торговлю — значения по умолчанию и строка в
    журнал.
    """
    import config
    with _lock:
        p = path()
        key = (p, _mtime(p))
        # Умолчания берутся из config, поэтому кэш помнит и его: проверки
        # перезагружают config с другими переменными окружения.
        if (not force and _cache['data'] is not None and _cache['key'] == key
                and _cache.get('config') is config):
            return _cache['data']
        if key[1] is None and _migrate(p):
            key = (p, _mtime(p))
        data = _defaults()
        if key[1] is not None:
            try:
                with open(p, encoding='utf-8') as fh:
                    _apply(data, json.load(fh))
            except Exception as exc:                   # noqa: BLE001
                log(f"⚠️ {FILE_NAME} нечитаем ({exc}) — счета на значениях по умолчанию")
                data = _defaults()
        _cache['key'], _cache['data'], _cache['config'] = key, data, config
        return data


def save(changes, record=True):
    """
    Применяет правку счетов и возвращает итог. Принимает частичный набор:
    панель шлёт только то, что трогали. record=False — запись в историю делает
    вызывающий (settings_store пишет одну запись на всю правку настроек).
    """
    with _lock:
        before = json.loads(json.dumps(load()))
        data = _apply(json.loads(json.dumps(load())), changes)
        p = path()
        if not _write(p, data):
            return data
        import config
        _cache['key'], _cache['data'], _cache['config'] = (p, _mtime(p)), data, config
    if record:
        write_history(before, data)
    return data


def _write(p, data):
    """Через временный файл и атомарную замену (CLAUDE.md, «Данные»)."""
    try:
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception as exc:                       # noqa: BLE001
        log(f"⚠️ Не удалось сохранить счета ({FILE_NAME}): {exc}")
        return False


def _migrate(p):
    """
    Перенос денег из прежних настроек стратегий (runtime_settings.json).
    True — перенесено и записано. Прежний файл не меняется; его копия
    остаётся рядом (BACKUP_NAME) — на случай отката кода.
    """
    folder = os.path.dirname(p)
    legacy = os.path.join(folder, LEGACY_NAME)
    try:
        with open(legacy, encoding='utf-8') as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(stored, dict):
        return False
    moved = {}
    for code in registry.codes():
        item = stored.get(code)
        if isinstance(item, dict):
            picked = {field: item[field] for field in FIELDS if field in item}
            if picked:
                moved[code] = picked
    portfolio = stored.get(PORTFOLIO)
    if isinstance(portfolio, dict):
        picked = {field: portfolio[field] for field in PORTFOLIO_FIELDS if field in portfolio}
        if picked:
            moved[PORTFOLIO] = picked
    if not moved:
        return False
    data = _apply(_defaults(), moved)
    backup = os.path.join(folder, BACKUP_NAME)
    if not os.path.exists(backup):
        try:
            shutil.copyfile(legacy, backup)
        except OSError as exc:
            log(f"⚠️ копия прежних настроек не сделана ({exc})")
    if not _write(p, data):
        return False
    _append_history([{'field': 'Счета.перенос',
                      'from': f'настройки стратегий ({LEGACY_NAME})',
                      'to': f'счета стратегий ({FILE_NAME})'}])
    log('💼 Деньги стратегий перенесены в счета (' + FILE_NAME + '): ' + '; '.join(
        f"{code} {'вкл' if data[code]['enabled'] else 'ВЫКЛ'} риск {data[code]['risk_pct']}% "
        f"депозит {data[code]['deposit']:,.0f} стороны {data[code]['sides']}"
        for code in registry.codes()))
    return True


# ── История: та же, что у настроек оператора ────────────────────────────────

def _flatten(data):
    flat = {}
    for section, values in (data or {}).items():
        if isinstance(values, dict):
            for field, value in values.items():
                flat[f'{section}.{field}'] = value
    return flat


def write_history(before, after):
    """Что изменилось и когда — только отличия (как settings_store)."""
    old, new = _flatten(before), _flatten(after)
    changes = [{'field': key, 'from': old.get(key), 'to': new[key]}
               for key in new if old.get(key) != new[key]]
    if changes:
        _append_history(changes)


def _append_history(changes):
    record = {'at': datetime.now().isoformat(timespec='seconds'), 'changes': changes}
    try:
        with open(os.path.join(_data_dir(), HISTORY_NAME), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError as exc:
        log(f"⚠️ не удалось записать историю настроек: {exc}")


# ── Точечные запросы ────────────────────────────────────────────────────────

def enabled(strategy):
    """Допущена ли стратегия к торговле на своём счёте."""
    return bool(load().get(strategy, {}).get('enabled', True))


def risk_pct(strategy):
    """Риск сделки, % депозита счёта (на тесте у всех 1%)."""
    import config
    return float(load().get(strategy, {}).get('risk_pct', config.RISK_PER_TRADE))


def deposit(strategy):
    """Стартовый депозит счёта."""
    import config
    return float(load().get(strategy, {}).get('deposit', config.PAPER_START_BALANCE))


def sides(strategy):
    """Какие стороны разрешены: both, long или short."""
    return clean_sides(load().get(strategy, {}).get('sides'), 'both')


def allows(strategy, direction):
    """
    Можно ли открывать сделку в такую сторону.

    Направление приходит в разных написаниях: LONG и SHORT, BULLISH и BEARISH у
    SMC. Сравнивать напрямую нельзя — фильтр по 'SHORT' не узнал бы шорт SMC и
    молча выпустил бы его при выключенных шортах.
    """
    allowed = sides(strategy)
    if allowed == 'both':
        return True
    value = str(direction or '').upper()
    is_long = value in ('LONG', 'BULLISH', 'BUY')
    is_short = value in ('SHORT', 'BEARISH', 'SELL')
    if not (is_long or is_short):
        return True                       # непонятное направление не режем
    return is_long if allowed == 'long' else is_short


def max_slots(strategy):
    """Предел одновременных позиций стратегии. UNLIMITED (0) — без предела."""
    value = load().get(strategy, {}).get('max_slots')
    return _default_slots() if value is None else int(value)


def slots_free(strategy, used):
    """
    Сколько ещё можно открыть. None — предела нет.

    Отдельная функция, а не вычитание на месте: «без предела» закодировано
    нулём, и `budget - used` дало бы при нуле отрицательное число, то есть
    «слоты заняты» — ровно противоположный смысл.
    """
    budget = max_slots(strategy)
    return None if budget <= UNLIMITED else budget - int(used)


def slots_label(strategy, used):
    """«3/8» или «3 (без предела)» — строка для журнала."""
    budget = max_slots(strategy)
    return f'{used} (без предела)' if budget <= UNLIMITED else f'{used}/{budget}'


def max_same_direction(strategy):
    """
    Предел позиций стратегии в одну сторону (0 — без предела): заданный на
    счёте, иначе — с каким стратегия измерена (strategy_profile).
    """
    value = load().get(strategy, {}).get('max_same_direction')
    if value is not None:
        return int(value)
    import strategy_profile
    return strategy_profile.max_same_direction(strategy)


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
    очереди.
    """
    return float(load().get(PORTFOLIO, {}).get('daily_loss_pct', 0) or 0)


def describe(strategy):
    """Счёт стратегии одним словарём — для журнала при старте и для панели."""
    return {'enabled': enabled(strategy), 'deposit': deposit(strategy),
            'risk_pct': risk_pct(strategy), 'sides': sides(strategy),
            'max_slots': max_slots(strategy),
            'max_same_direction': max_same_direction(strategy)}


# ── Решение счёта по сетапу ─────────────────────────────────────────────────

def decide(strategy, signal, balance):
    """
    Решение счёта по сетапу стратегии: (сигнал, None) — счёт берёт его, в
    params вписаны деньги; (None, причина) — отказ.

    Деньги — поля, которые читают исполнители (strategies/contract.MONEY_KEYS):
      risk_pct            % депозита на сделку — риск счёта (у всех стратегий
                          один; множителей у сетапа нет — решение владельца
                          08.10.2026);
      max_same_direction  предел позиций стратегии в одну сторону;
      risk_amount, position_size — размер по плану: от депозита balance и
                          расчётного входа; исполнитель пересчитает его от
                          цены заполнения и своего текущего депозита.
    Сигнал меняется на месте и возвращается тот же объект.
    """
    direction = (signal.get('setup') or {}).get('type') or signal.get('direction')
    if not allows(strategy, direction):
        return None, f"{direction} пропущен — на счёте разрешены только «{sides(strategy)}»"
    params = signal.setdefault('params', {})
    risk = risk_pct(strategy)
    params['risk_pct'] = risk
    params['max_same_direction'] = max_same_direction(strategy)
    amount = float(balance or 0) * (risk / 100)
    try:
        distance = abs(float(params['entry']) - float(params['stop_loss']))
    except (KeyError, TypeError, ValueError):
        distance = 0.0
    params['risk_amount'] = amount
    params['position_size'] = amount / distance if distance else 0.0
    return signal, None
