"""
Объявления биржи — сырые данные общего слоя (28.09.2026).

Листинги, делистинги, обслуживание, новые продукты Bybit — текст, где у
языковой модели нет замены кодом. По правилу проекта новый источник сначала
логируется, потом используется (CLAUDE.md, «Данные»): здесь только запись,
как есть, в DATA_DIR/news/bybit.jsonl. Никто из стратегий их пока не читает.

Раз в час — последние 20 объявлений, новые (по url) дописываются. Отказ
источника — строка в журнал бота, торговля от него не зависит.
"""
import json
import os
import time

import config
from data import sources
from logger import log

# Адрес Bybit — в реестре источников (data/sources.py), путь — здесь.
PATH = '/v5/announcements/index?locale=en-US&limit=20'
_last_poll = {'ts': 0}
_seen = set()


def _path():
    return os.path.join(config.DATA_DIR, 'news', 'bybit.jsonl')


def _load_seen():
    if _seen:
        return
    try:
        with open(_path(), encoding='utf-8') as fh:
            for line in fh:
                try:
                    _seen.add(json.loads(line).get('url'))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass


def poll(now_s=None, every_s=3600):
    """Дописывает новые объявления не чаще раза в every_s. -> сколько записано."""
    now_s = time.time() if now_s is None else now_s
    if now_s - _last_poll['ts'] < every_s:
        return 0
    _last_poll['ts'] = now_s
    try:
        items = (sources.get('bybit', PATH).get('result') or {}).get('list') or []
    except Exception as exc:                          # noqa: BLE001
        log(f'   новости Bybit: не прочитаны ({exc})')
        return 0
    _load_seen()
    fresh = [x for x in items if x.get('url') and x['url'] not in _seen]
    if not fresh:
        return 0
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(_path(), 'a', encoding='utf-8') as fh:
        for x in reversed(fresh):
            x = dict(x, logged_ts=int(now_s * 1000))
            fh.write(json.dumps(x, ensure_ascii=False) + '\n')
            _seen.add(x['url'])
    return len(fresh)
