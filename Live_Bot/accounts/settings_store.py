"""
Настройки, меняемые на ходу из дашборда и Telegram.

Отдельный слой поверх `.env`, а не замена ему. `.env` задаёт, с чем бот
СТАРТУЕТ; здесь — то, что оператор меняет во время работы. Файлы читаются на
каждом цикле, поэтому изменение вступает в силу без перезапуска. Уже открытые
позиции при этом не трогаются: их размер и стоп посчитаны при входе.

С 08.10.2026 (реорганизация, этап 2) у настроек РАЗНЫЕ ХОЗЯЕВА, а этот модуль —
одно окно к ним для панели и Telegram:
  - деньги — счёт стратегии (accounts/paper.py, файл accounts.json): допущена
    ли стратегия, риск на сделку, депозит, стороны, позиций всего и в одну
    сторону, пределы портфеля;
  - правила решений стратегии — strategies/settings.py (минимальный стоп,
    критик ИИ), файл runtime_settings.json;
  - уведомления и выбор биржи — здесь же, в runtime_settings.json.
load() отдаёт всё одним словарём в прежнем виде (раздел на стратегию, PORTFOLIO,
EXCHANGE, NOTIFY), save() делит правку между хозяевами и пишет ОДНУ запись в
историю настроек. Стратегии этот модуль не читают: денег они не знают, а свои
правила берут из strategies/settings.py.

Все значения ограничиваются диапазоном при записи. Опечатка в поле «риск»
(5 вместо 0.5) — это не косметика, а десятикратный размер позиции, поэтому
проверка стоит на входе, а не на совести того, кто вводит.
"""

import json
from datetime import datetime
import os
import threading

from infra import config
from infra.logger import log
from accounts import paper as accounts
from strategies import settings as strategy_settings

SETTINGS_FILE = os.path.join(config.DATA_DIR, 'runtime_settings.json')
# RSIBB добавлена ЧЕТВЁРТОЙ и на особом положении: она единственная не прошла
# приёмку проекта. Её край положителен на обоих периодах, но интервал накрывает
# ноль, поэтому она включена ради данных ВНЕ выборки, а не потому что доказана.
# SMCS (SMC-структура 4ч) добавлена 02.10.2026 тоже кандидатом: на парах
# пула её плюс держится на всех трёх периодах, но строгую приёмку VAL она не
# прошла (smcs/params.py, research/smcz/PROTOCOL.md).
# FIB12 (Фибо 12ч) добавлена 07.10.2026: ФИБО, собранная с нуля, прошла приёмку
# протокола (research/fibz/PROTOCOL.md); торгуется рядом со старой ФИБО.
# С 08.10.2026 список — из реестра стратегий (strategies/registry.py).
from strategies import registry as _registry
STRATEGIES = _registry.codes()

UNLIMITED = accounts.UNLIMITED

# Границы разумного — у хозяев полей; здесь одним словарём для панели.
LIMITS = {**accounts.LIMITS, **strategy_settings.LIMITS}

PORTFOLIO = accounts.PORTFOLIO
PORTFOLIO_FIELDS = accounts.PORTFOLIO_FIELDS

# Выбранная биржа. Отдельным разделом: она не принадлежит ни одной стратегии и
# не является пределом риска. КЛЮЧИ ЗДЕСЬ НЕ ХРАНЯТСЯ И НЕ ПРИНИМАЮТСЯ — они
# живут в .env. У дашборда нет пароля (см. docs/Перед_реальным_счётом.md), и
# приём секретов по открытому HTTP был бы дырой, а не удобством. Через панель
# можно только ПЕРЕКЛЮЧИТЬСЯ на биржу, ключи которой уже прописаны.
EXCHANGE = 'EXCHANGE'
EXCHANGES = ('bybit', 'bingx')

# Уведомления: что присылать и куда. Раздельно по событиям и каналам, потому
# что «слишком много уведомлений» и «слишком мало» — разные беды у одного и
# того же человека: сообщение о каждом входе на телефон раздражает, а
# сообщение об ошибке пропускать нельзя.
#
# По умолчанию включено всё — так вело себя приложение до появления этой
# настройки. Молча выключить часть сообщений значило бы, что человек
# перестанет что-то получать и не поймёт почему.
NOTIFY = 'NOTIFY'
# С 26.09.2026 ещё четыре: цель взята (tp_hit), стоп в безубытке (breakeven),
# отказ плана ИИ (llm_rejected — раньше шёл под одним ключом с принятым
# планом, и выключить поток отказов, не потеряв планы, было нельзя) и запуск
# бота (service — раньше уходил без спроса после каждой выкатки).
# С 08.10.2026 — инструкции по торговым счетам без API (account_orders): что
# поставить, снять, перенести и закрыть руками (accounts/manual.py).
NOTIFY_EVENTS = ('trade_opened', 'trade_closed', 'error', 'daily', 'llm_setup',
                 'plan_dropped', 'tp_hit', 'breakeven', 'llm_rejected', 'service',
                 'account_orders')
NOTIFY_CHANNELS = ('telegram',)

# Поля раздела стратегии в порядке, в котором их видели панель и журнал до
# разделения хозяев: деньги (счёт) вперемешку с правилами (стратегия).
_STRATEGY_FIELDS = ('enabled', 'risk_pct', 'min_stop_pct', 'deposit', 'max_slots', 'sides',
                    'critic', 'notify', 'max_same_direction')

_lock = threading.Lock()
_cache = None
_mtime = None
_money = None          # словарь счетов, из которого собран _cache


def _own_defaults():
    """Поля этого файла: правила стратегий, уведомления, биржа."""
    base = {name: {**strategy_settings.defaults(),
                   # Присылать ли в Telegram сделки ЭТОЙ стратегии (вход, цели,
                   # безубыток, выход, снятые заявки). Торговлю не трогает —
                   # только сообщения: у фибо входов много, у ИИ мало, и
                   # человеку может быть нужен поток одной стратегии без шума
                   # другой.
                   'notify': True}
            for name in STRATEGIES}
    base[EXCHANGE] = {'name': (config.EXCHANGE_NAME or 'bybit').lower()}
    base[NOTIFY] = {f'{event}_{channel}': True
                    for event in NOTIFY_EVENTS for channel in NOTIFY_CHANNELS}
    return base


def _apply_own(data, changes):
    """Правка (или записанный файл) поверх своих полей — с проверкой."""
    changes = changes if isinstance(changes, dict) else {}
    chosen = ((changes.get(EXCHANGE) or {}).get('name') or '').lower()
    if chosen in EXCHANGES:
        data[EXCHANGE]['name'] = chosen
    notify = changes.get(NOTIFY) or {}
    for key in data[NOTIFY]:
        if key in notify:
            data[NOTIFY][key] = bool(notify[key])
    for name in STRATEGIES:
        item = changes.get(name)
        if not isinstance(item, dict):
            continue
        data[name] = {**strategy_settings.section(item, data[name]),
                      'notify': bool(item['notify']) if 'notify' in item else data[name]['notify']}
    return data


def _compose(own, money):
    """Один словарь в прежнем виде: раздел на стратегию, портфель, биржа,
    уведомления."""
    data = {}
    for name in STRATEGIES:
        merged = {**money.get(name, {}), **own.get(name, {})}
        data[name] = {field: merged.get(field) for field in _STRATEGY_FIELDS}
    data[PORTFOLIO] = dict(money.get(PORTFOLIO, {}))
    data[EXCHANGE] = dict(own[EXCHANGE])
    data[NOTIFY] = dict(own[NOTIFY])
    return data


def load(force=False):
    """
    Текущие настройки. Перечитывает файлы, только если они изменились —
    вызывается каждый цикл и не должен превращаться в дисковую нагрузку.
    """
    global _cache, _mtime, _money
    money = accounts.load(force=force)
    with _lock:
        try:
            stamp = os.path.getmtime(SETTINGS_FILE)
        except OSError:
            stamp = None

        key = (SETTINGS_FILE, stamp)
        if _cache is not None and not force and _mtime == key and _money is money:
            return _cache

        own = _own_defaults()
        if stamp is not None:
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as fh:
                    _apply_own(own, json.load(fh))
            except Exception as exc:
                log(f"⚠️ runtime_settings.json нечитаем ({exc}) — берём значения из .env")
                own = _own_defaults()

        _cache, _mtime, _money = _compose(own, money), key, money
        return _cache


def save(changes):
    """
    Применяет изменения и возвращает итоговые настройки.

    Принимает частичный набор: дашборд шлёт только то, что трогали. Деньги
    уходят счёту (accounts/paper.py), остальное — в runtime_settings.json; в
    историю ложится одна запись на всю правку. Денежных полей в
    runtime_settings.json после записи нет: их единственное место — счёт.
    """
    global _cache, _mtime, _money
    changes = changes if isinstance(changes, dict) else {}
    before = json.loads(json.dumps(load()))   # снимок ДО правки — для истории

    accounts.save(changes, record=False)
    own = _apply_own(_apply_own(_own_defaults(), before), changes)

    with _lock:
        try:
            tmp = SETTINGS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(own, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
        except Exception as exc:
            log(f"⚠️ Не удалось сохранить настройки: {exc}")
        _cache, _mtime, _money = None, None, None

    data = load()
    log('⚙️ Настройки изменены: ' + ', '.join(
        f"{name} {'вкл' if data[name]['enabled'] else 'ВЫКЛ'} "
        f"риск {data[name]['risk_pct']}% стоп>={data[name]['min_stop_pct']}% "
        f"стороны {data[name]['sides']}"
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
# Деньги — у счёта стратегии; здесь они повторены для панели, Telegram,
# doctor и исполнителей (этап 5 переведёт исполнителей на решение счёта).

enabled = accounts.enabled
risk_pct = accounts.risk_pct
deposit = accounts.deposit
sides = accounts.sides
allows = accounts.allows
max_slots = accounts.max_slots
slots_free = accounts.slots_free
slots_label = accounts.slots_label
max_same_direction = accounts.max_same_direction
portfolio_risk_pct = accounts.portfolio_risk_pct
portfolio_max_positions = accounts.portfolio_max_positions
daily_loss_pct = accounts.daily_loss_pct


def critic_enabled():
    """
    Второе мнение о плане модели: включено оператором с панели?

    Тумблер живёт в настройках, а не в .env: выключить критика на неделю
    ради замера аналитика без него — решение оператора, а не перезапуск.
    Настройка недоступна — берётся config.LLM_CRITIC.
    """
    try:
        return bool(load().get('LLM', {}).get('critic', True))
    except Exception:                              # noqa: BLE001
        return bool(getattr(config, 'LLM_CRITIC', True))


def min_stop_pct(strategy):
    """Минимальная дистанция стопа, в ДОЛЯХ цены (а не в процентах)."""
    value = load().get(strategy, {}).get('min_stop_pct')
    if value is None:
        return float(config.MIN_SL_PERCENT)
    return float(value) / 100.0


def exchange_name():
    """Выбранная биржа. Ключи здесь не хранятся — только выбор."""
    return (load().get(EXCHANGE, {}).get('name')
            or (config.EXCHANGE_NAME or 'bybit')).lower()


def notify_on(event, channel):
    """
    Присылать ли уведомление о событии в этот канал.

    Неизвестное событие считается разрешённым: забытая настройка не должна
    молча гасить сообщение, которое кто-то рассчитывал получить.
    """
    key = f'{event}_{channel}'
    section = load().get(NOTIFY) or {}
    return bool(section.get(key, True))


def notify_strategy(strategy):
    """Присылать ли сообщения о сделках этой стратегии. Незнакомая — да."""
    return bool(load().get(strategy, {}).get('notify', True))
