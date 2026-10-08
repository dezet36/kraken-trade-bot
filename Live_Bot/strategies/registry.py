"""
Реестр стратегий: одна запись — одна стратегия.

ЗАЧЕМ. До 08.10.2026 стратегию встраивали правкой ~12 общих файлов: свой кортеж
имён в настройках, в брокере, в Telegram, в проверке готовности, в панели; своя
ветка в цикле бота (дважды), в сборке сигнала, в профиле исполнения, в разметке
графика. Пропуск одного места не падал, а тихо выключал часть стратегии (так
уровни однажды торговали чужой сетап под своим именем).

Теперь у стратегии — свой пакет и адаптер с единым набором функций, а здесь —
запись о ней. Общие модули берут список стратегий и вызовы только отсюда.

ЕДИНЫЙ НАБОР ФУНКЦИЙ АДАПТЕРА (модуль из поля `adapter`):
    scan(pairs, gate, client=None) -> [кандидат]
        кандидаты по парам; gate — проверка «пара свободна / пауза»;
    build_signal(candidate) -> (сигнал, свечи для графика) | (None, None)
        сетап в общем виде БЕЗ ДЕНЕГ (strategies/contract.py): депозита и
        риска стратегия не знает; стороны, риск, предел в одну сторону и
        размер решает счёт (accounts/paper.py);
    profile() -> dict
        величины исполнения и жизни сетапа (срок заявки, пауза после выхода,
        предел издержек, удержание, снятие у цели, налив за рынком, смещение
        лимита) и предел позиций в одну сторону, с которым стратегия измерена
        (счёт берёт его по умолчанию) — чего нет, то берётся общее
        (strategy_profile);
    geometry(signal, g)
        разметка сетапа для графиков через помощники g (setup_geometry).

Модуль данных этого реестра не трогает биржу и деньги: только описание.
План — docs/Архитектура_модули_2026-10-08.md, этап 1.
"""

import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Strategy:
    code: str                 # внутреннее имя: в журналах, настройках, кнопках
    title: str                # имя на панели
    short: str                # имя в Telegram
    adapter: str              # модуль адаптера
    signal_keys: tuple        # разделы сигнала, по которым узнаётся стратегия
    color_light: str          # цвет на панели, светлая тема
    color_dark: str           # тёмная тема
    in_live_cycle: bool = True     # торгуется ли в боевом цикле (ИИ — только бумага)
    detect_rank: int = 5      # порядок узнавания по сигналу (меньше — раньше)


# Порядок — порядок обхода в цикле бота и показа на панели и в Telegram.
REGISTRY = (
    Strategy('FIBO', 'Фибоначчи', 'Фибо', 'strategies.fibo', ('zone_a', 'zone_b'),
             '#2a78d6', '#3987e5', detect_rank=9),
    Strategy('SMC', 'Smart Money', 'SMC', 'strategy_smc', ('smc',), '#eb6834', '#d95926'),
    Strategy('LEVELS', 'Уровни', 'Уровни', 'strategy_levels', ('levels',), '#17a398', '#2bbfb2'),
    Strategy('RSIBB', 'Боллинджер', 'Боллинджер', 'strategy_rsibb', ('rsibb',), '#664089', '#4c36be'),
    Strategy('LLM', 'ИИ', 'ИИ', 'strategy_llm', ('llm',), '#c48a12', '#e5a93a', in_live_cycle=False,
             detect_rank=0),
    Strategy('SMCS', 'SMC-структура 4ч', 'SMC 4ч', 'strategy_smcs', ('smcs',), '#56606b', '#a7b1bc'),
    Strategy('FIB12', 'Фибо 12ч', 'Фибо 12ч', 'strategy_fib12', ('fib12',), '#a3367a', '#e07ab5'),
)

_BY_CODE = {s.code: s for s in REGISTRY}


def codes():
    """Коды всех стратегий в порядке обхода."""
    return tuple(s.code for s in REGISTRY)


def live_codes():
    """Коды стратегий боевого цикла."""
    return tuple(s.code for s in REGISTRY if s.in_live_cycle)


def get(code):
    """Запись стратегии или None."""
    return _BY_CODE.get(code)


def adapter(code):
    """Модуль адаптера стратегии (импорт при первом обращении)."""
    entry = _BY_CODE.get(code)
    if entry is None:
        raise KeyError(f'стратегии {code} нет в реестре')
    return importlib.import_module(entry.adapter)


def strategy_of(signal):
    """
    Чья это сделка — по разделам сигнала; неизвестный сигнал — пустая строка
    (разметка будет пустой, график не сломается). Порядок узнавания —
    detect_rank: ИИ раньше всех (сигнал ИИ в режиме «правила» несёт и раздел
    SMC), Фибоначчи — последней (её признак — зоны A и B).
    """
    for entry in sorted(REGISTRY, key=lambda s: s.detect_rank):
        if any(signal.get(key) for key in entry.signal_keys):
            return entry.code
    return ''


def titles():
    return {s.code: s.title for s in REGISTRY}


def shorts():
    return {s.code: s.short for s in REGISTRY}


def as_json():
    """Описание стратегий для панели (имена и цвета)."""
    return [{'code': s.code, 'title': s.title, 'short': s.short,
             'color_light': s.color_light, 'color_dark': s.color_dark} for s in REGISTRY]
