"""
Накопление снимков цен. Копим своё, потому что чужого нет.

ЗАЧЕМ ЭТО НУЖНО. История цен у площадки живёт 30-40 суток — измерено: на
2026-07-09 она есть, на 2026-06-24 её уже нет, а события 2025 года доступны с
исходами, но без единой точки цены. Значит любой замер «обыграли бы мы рынок»
упирается в месячное окно, а месяц с хвостозависимым результатом — это признак,
а не доказательство.

Единственный выход — начать копить с первого дня. Ровно так уже сделано с
открытым интересом и дельтой на бирже, и это единственный честный путь, когда
история недоступна.

ЧТО ИМЕННО ПИШЕТСЯ. Снимок цен всех корзин события в момент наблюдения плюс
наша оценка на тот же момент. Оценка сохраняется ВМЕСТЕ с ценой, а не считается
задним числом: параметры модели меняются, и пересчёт показал бы, что мы «знали»
то, чего в тот день не знали.

Файлы построчные (jsonl) и растут вечно, поэтому лежат вне репозитория.
"""

import json
import os
import time

import config

# Данные лежат ОТДЕЛЬНО от кода пакета: складывать растущие файлы рядом с
# модулями значит рано или поздно закоммитить наблюдения вместе с правкой.
DIR = os.path.join(config.DATA_DIR, 'polymarket_data')
SNAPSHOTS = os.path.join(DIR, 'snapshots.jsonl')
DECISIONS = os.path.join(DIR, 'decisions.jsonl')


def _append(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')


def _stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def save_snapshot(signals):
    """Снимок расхождений. Пустой список ничего не пишет."""
    if not signals:
        return 0
    at = _stamp()
    for s in signals:
        market = s.get('market') or {}
        _append(SNAPSHOTS, {
            'at': at,
            'market_id': market.get('id'),
            'question': market.get('question'),
            'end': market.get('endDate'),
            'category': market.get('feeType'),
            'event': ((market.get('events') or [{}])[0]).get('id'),
            'price': s.get('price'),
            'model': s.get('model'),
            'edge': s.get('edge'),
            'cost': s.get('cost'),
            'liquidity': s.get('liquidity'),
            'city': s.get('city'),
            'forecast_c': s.get('forecast_c'),
        })
    return len(signals)


def save_decision(signal, decision, strategy):
    """
    Решение вместе с его основанием.

    Пишутся и отказы тоже: без них нельзя отличить «сигналов не было» от
    «сигналы были, но все отсеяны», а это разные болезни.
    """
    market = signal.get('market') or {}
    _append(DECISIONS, {
        'at': _stamp(),
        'strategy': strategy,
        'market_id': market.get('id'),
        'question': market.get('question'),
        'action': decision.action,
        'size_usd': decision.size_usd,
        'kelly': round(decision.kelly, 4),
        'reason': decision.reason,
        'price': signal.get('price'),
        'model': signal.get('model'),
        'details': decision.details,
    })


def read(path, limit=None):
    """Строки файла, самые свежие последними. Отсутствие файла — пустой список."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:] if limit else rows
