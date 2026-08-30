"""
Сигналы, которые бот получил и НЕ взял.

ЗАЧЕМ. Воронка отсева живёт в памяти до следующего цикла — так и написано в
scan_report: «словарь в памяти, без диска». Значит бот десятки раз за цикл
решает не входить, и эти решения исчезают. Видно только то, что он сделал, и
никогда — то, от чего отказался.

Стало срочным 29 августа 2026, когда появился предел издержек на вход. По
локальному журналу он отсекает 17% сетапов у фибо и 80% у SMC — а проверить,
правильно ли, нечем. Может, он выбрасывает как раз прибыльные. Фильтр, чьё
действие нельзя измерить, — это ровно то, за что в этом проекте уже досталось
зоне B и развилке THIN_STOP.

ЗАПИСЫВАЮТСЯ НЕ ВСЕ ОТКАЗЫ, И ЭТО НАМЕРЕННО. «По паре уже есть позиция» и
«цена не дошла до зоны» случаются десятки раз за цикл по каждой паре, и файл
распух бы, ничего не объясняя: это не отказ от сделки, а отсутствие сетапа.

Пишутся отказы ПРЕДОХРАНИТЕЛЕЙ: сетап был готов, цена и стоп известны, и его
отвергло правило. Только такие можно потом прогнать и спросить, что было бы.
"""

import csv
import os

import config
from logger import log

CSV_PATH = os.path.join(config.DATA_DIR, 'refused.csv')

COLUMNS = [
    'at', 'strategy', 'pair', 'direction',
    'entry', 'stop_loss', 'tp1', 'rr',
    # Чем именно отвергнут: предел издержек, предел портфеля, направленный
    # кэп, дневной стоп-кран, кулдаун.
    'gate', 'detail',
    # Доля риска, уходящая в комиссии. Пишется всегда, когда известна: по ней
    # и проверяется, верно ли отсекает предел издержек.
    'cost_share_pct',
]


def record(strategy, signal, gate, detail='', cost_share=''):
    """
    Пишет один отказ. Молча: запись наблюдений не имеет права мешать торговле.

    `gate` — короткое имя правила, `detail` — его собственное объяснение с
    числами, как оно уже уходит в журнал бота.
    """
    try:
        params = (signal or {}).get('params') or {}
        setup = (signal or {}).get('setup') or {}
        targets = params.get('tp_targets') or []
        row = {
            'at': _now_iso(),
            'strategy': strategy,
            'pair': signal.get('trading_pair', ''),
            'direction': setup.get('type', ''),
            'entry': params.get('entry', ''),
            'stop_loss': params.get('stop_loss', ''),
            'tp1': (targets[0] if targets else params.get('take_profit_1', '')),
            'rr': params.get('rr', ''),
            'gate': gate,
            'detail': str(detail)[:200],
            'cost_share_pct': cost_share,
        }
        fresh = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
        with open(CSV_PATH, 'a', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction='ignore')
            if fresh:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Отказ не записан: {exc}')


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
