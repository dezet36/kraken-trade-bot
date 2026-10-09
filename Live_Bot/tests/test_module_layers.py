"""
Слои модулей — сторож реорганизации (docs/Архитектура_модули_2026-10-08.md).

Связи между модулями — в одну сторону:
    платформа ← данные ← анализ ← стратегии ← исполнение ← счета ← интерфейсы ← бот
Модуль нижнего слоя не импортирует верхний: данные не знают про счета, стратегии —
про деньги, анализ — про стратегии.

КАК РАБОТАЕТ. У каждого модуля бота записан слой, в который он переезжает
(LAYER). Импорт «вверх» — нарушение. Нарушения, которые есть сейчас, записаны в
KNOWN и исправляются по этапам плана; тест падает, если
  - появилось НОВОЕ нарушение (новый код не должен добавлять путаницы);
  - нарушение из KNOWN исправлено, а из списка не вычеркнуто (список не врёт);
  - появился модуль без слоя (его место надо решить сразу).
Код внутри новых пакетов (infra, data, analysis, strategies, execution, accounts,
control) нарушений не имеет вовсе: KNOWN — только для старых модулей.
"""

import ast
import os

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ORDER = ('infra', 'data', 'analysis', 'strategies', 'execution', 'accounts', 'control', 'app')

PACKAGES = {'infra': 'infra', 'data': 'data', 'analysis': 'analysis', 'strategies': 'strategies',
            'execution': 'execution', 'accounts': 'accounts', 'control': 'control'}

# Куда переезжает каждый старый модуль (раздел 3 плана). Пакеты стратегий — в
# «стратегии»; smc целиком — пока там же (его часть I уйдёт в анализ на этапе 4).
LAYER = {
    # платформа
    # config, logger, csv_journal, params_env, error_log, updater, llm_local,
    # llm_worker — в infra/ (этап 10, часть 8)
    'llm_server': 'infra',
    # данные
    # анализ
    # market_structure, llm_market — в analysis/ (этап 10, часть 10)
    'llm_context': 'analysis',
    # стратегии
    # ИИ, ФИБО и общие модули стратегий — в strategies/ (этап 10, часть 6)
    # исполнение и учёт
    # exit_plan, follow_up, setup_journal, refused — в execution/ (этап 10, часть 7)
    # paper_broker, trade_manager, trade_journal, shadow — в execution/ (часть 9)
    # счета
    # интерфейсы — в control/ (этап 10, часть 5)
    # дирижёр цикла
    'bot': 'app',
}

# Нарушения на 08.10.2026. Исправляются по этапам — вычёркивать по мере
# исправления; добавлять сюда новые нельзя. Этап 1 убрал 2, этап 2 — 11
# (стратегии больше не читают деньги из настроек: их решает счёт).
KNOWN = {
    # Чтение рынка с этапа 3 — через реестр источников (data/sources.py); здесь
    # остаётся торговая половина exchange.py: выбор биржи ТОРГОВЛИ и ключи.
    # Разметка ИИ и общий слой структуры берут функции ядра smc: в нём структура
    # (часть I) и решения SMC (часть II) живут в одном классе MarketContext.
    # Ядро делится на «структуру → анализ» и «решения → стратегия» при переносе
    # файлов (этап 10), разметка режима планов — там же.
    # С переноса пакетов стратегий в strategies/ (этап 10, часть 1) — одной
    # записью на модуль: разметка ИИ читает пакеты liquidity и smc.
    ('llm_context', 'strategies'): 'этап 10: структура smc (часть I) и liquidity — в анализ',
    ('llm_server', 'strategies'): 'этап 10: сервис модели не знает про промт и грамматику',
    # Исполнители (брокер, боевой исполнитель, тень, журнал сделок) с этапа 10,
    # часть 9, не импортируют счета и интерфейсы: арифметика пределов и
    # издержек — execution/risk_gate и live_costs, сами пределы — крючок
    # portfolio_limits, Telegram — порт infra/outbox.
}


def _modules():
    """(имя модуля, путь, его слой) для всего кода бота, кроме тестов."""
    out = []
    for dp, dn, fn in os.walk(BOT):
        dn[:] = [d for d in dn if d not in ('__pycache__', 'tests', '.claude', '.pytest_cache')]
        rel = os.path.relpath(dp, BOT)
        top = rel.split(os.sep)[0] if rel != '.' else None
        for f in fn:
            if not f.endswith('.py'):
                continue
            name = (f[:-3] if top is None else top)
            layer = PACKAGES.get(top) if top in PACKAGES else LAYER.get(name)
            out.append((name if top is None else os.path.join(rel, f).replace(os.sep, '/'),
                        os.path.join(dp, f), layer, top))
    return out


def _local_imports(path, local):
    tree = ast.parse(open(path, encoding='utf-8').read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for n in names:
            head = n.split('.')[0]
            if head in local:
                found.add(head)
    return found


def violations():
    mods = _modules()
    local = set(LAYER) | set(PACKAGES)
    out, unknown = set(), []
    for name, path, layer, top in mods:
        if layer is None:
            unknown.append(name)
            continue
        own = top if top in PACKAGES else name
        for imp in _local_imports(path, local) - {own}:
            target = PACKAGES.get(imp) or LAYER.get(imp)
            if ORDER.index(target) > ORDER.index(layer):
                out.add((name, imp))
    return out, unknown


def test_every_module_has_a_layer():
    _, unknown = violations()
    assert not unknown, f'модулю не назначен слой (впиши в LAYER): {sorted(unknown)}'


def test_no_new_upward_imports():
    found, _ = violations()
    new = sorted(found - set(KNOWN))
    fixed = sorted(set(KNOWN) - found)
    assert not new, f'новый импорт «вверх по слоям»: {new}'
    assert not fixed, f'нарушение исправлено — вычеркни из KNOWN: {fixed}'


DATA_LAYER = {name for name, layer in LAYER.items() if layer == 'data'} | {'data'}


def test_strategies_read_data_only_through_analysis():
    """
    Стратегии берут рыночные данные через анализ (analysis/market.py,
    market_structure, market_regime…), а не у сборщиков и не с биржи: снимок
    рынка один для всех, и стратегия не знает, откуда он (этап 4).
    """
    local = set(LAYER) | set(PACKAGES)
    found = []
    for name, path, layer, top in _modules():
        if layer != 'strategies':
            continue
        hits = _local_imports(path, local) & DATA_LAYER
        if hits:
            found.append((name, sorted(hits)))
    assert not found, f'стратегия читает данные мимо анализа: {found}'


def test_new_packages_are_clean():
    found, _ = violations()
    dirty = sorted(v for v in found if '/' in v[0])
    assert not dirty, f'в новых пакетах импорт вверх: {dirty}'


if __name__ == '__main__':
    found, unknown = violations()
    print('нет слоя:', unknown)
    for v in sorted(found):
        print(f'    {v!r}: \'\',')
    print(len(found), 'нарушений')
