"""
Рынок в целом: USDT.D, BTC.D, TOTAL2 — по методике TradingView, но своими руками.

ЗАЧЕМ. Модель видит одну пару и BTC как ориентир, но не видит, куда идут
деньги во всём рынке: растёт ли доля стейблкоинов (выход в кэш — альты
слабее) и держит ли BTC долю (альтсезон или его отсутствие). Это фон, не
сигнал: он объясняет силу и слабость альтов, но не даёт уровней.

ПОЧЕМУ САМИ, А НЕ С АГРЕГАТОРА. TradingView не берёт USDT.D с бирж — он
считает его: капа монеты = цена × обращающееся предложение, TOTAL — сумма по
топ-125 монет, USDT.D = капа USDT ÷ TOTAL. Агрегаторы отдают то же с
задержкой, по своей методике и с лимитами запросов. Здесь формула та же, а
источники — по надёжности каждой части:
  цены        — Bybit, тикеры спота одним запросом, каждый цикл (5 мин);
  предложение — CoinGecko, раз в час с кэшем на диске: оно меняется днями,
                и прошлочасовое остаётся верным даже при отказе источника;
  монеты не с Bybit — цена CoinGecko из того же часового снимка (их доля мала);
  фонды и неторгуемые токены из списка исключаются (см. tradable).
Расхождение с графиком TradingView — в пределах десятых долей процента из-за
состава списка и источника предложения; динамика та же.

ПРАВИЛА НАДЁЖНОСТИ. Каждое число уходит в разметку с возрастом; старше
STALE_MIN минут — прочерк, не последнее известное. Отказ любого источника —
прочерк и строка в журнал, торговля от этого блока не зависит.
"""

import json
import os
import time
import urllib.request

import config
from logger import log

TOP = 125
SUPPLY_URL = ('https://api.coingecko.com/api/v3/coins/markets'
              f'?vs_currency=usd&order=market_cap_desc&per_page={TOP}&page=1')
SUPPLY_REFRESH_SEC = 3600          # предложение обновляется раз в час
SUPPLY_MAX_AGE_SEC = 48 * 3600     # старше двух суток — не считаем
COLLECT_EVERY_SEC = 290            # раз в цикл бота
STALE_MIN = 120                    # возраст, после которого блок — прочерк
KEEP_DAYS = 30

_last_collect = 0.0
_supply_cache = None               # {'at': ts, 'coins': [...]}


def store_path():
    return os.path.join(config.DATA_DIR, 'market_cap.jsonl')


def supply_path():
    return os.path.join(config.DATA_DIR, 'market_cap_supply.json')


# ── Предложение (медленная часть) ────────────────────────────────────────────

def _load_supply():
    global _supply_cache
    if _supply_cache is None:
        try:
            with open(supply_path(), encoding='utf-8') as fh:
                _supply_cache = json.load(fh)
        except (OSError, ValueError):
            _supply_cache = {'at': 0, 'coins': []}
    return _supply_cache


def _fetch_supply(timeout=20):
    req = urllib.request.Request(SUPPLY_URL, headers={'User-Agent': 'kraken-bot/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode('utf-8'))
    coins = []
    for c in raw or []:
        try:
            supply = float(c.get('circulating_supply') or 0)
            price = float(c.get('current_price') or 0)
            cap = float(c.get('market_cap') or 0)
            volume = float(c.get('total_volume') or 0)
        except (TypeError, ValueError):
            continue
        if supply <= 0 or price <= 0:
            continue
        coins.append({'id': c.get('id'), 'symbol': str(c.get('symbol') or '').upper(),
                      'supply': supply, 'price': price,
                      'volume_pct': (volume / cap * 100) if cap > 0 else 0.0})
    if len(coins) < TOP // 2:
        raise RuntimeError(f'CoinGecko отдал {len(coins)} монет')
    return coins


def supply(now=None, fetch=None):
    """Список топ-монет с предложением; обновляется раз в час, иначе — кэш."""
    global _supply_cache
    fetch = fetch or _fetch_supply
    now = now if now is not None else time.time()
    cache = _load_supply()
    if now - float(cache.get('at') or 0) < SUPPLY_REFRESH_SEC and cache.get('coins'):
        return cache
    try:
        coins = fetch()
    except Exception as exc:                       # noqa: BLE001
        log(f'   рынок в целом: предложение монет не обновлено — {exc}')
        return cache
    _supply_cache = {'at': now, 'coins': coins}
    try:
        tmp = supply_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(_supply_cache, fh)
        os.replace(tmp, supply_path())
    except OSError:
        pass
    return _supply_cache


# ── Цены (быстрая часть) ─────────────────────────────────────────────────────

def _bybit_spot_tickers(client):
    """{'BTCUSDT': {'last': цена, 'pct24': доля}} по всему споту Bybit одним запросом."""
    raw = client.publicGetV5MarketTickers({'category': 'spot'})
    out = {}
    for t in ((raw or {}).get('result') or {}).get('list') or []:
        try:
            out[t['symbol']] = {'last': float(t['lastPrice']),
                                'pct24': float(t.get('price24hPcnt') or 0) * 100}
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError('тикеры спота пусты')
    return out


# Стейблкоины: цена ≈ 1, и торгуемой пары к USDT у самого USDT нет.
STABLE = {'USDT', 'USDC', 'DAI', 'FDUSD', 'USDE', 'TUSD', 'PYUSD', 'USDS', 'USD1', 'BUSD', 'USDD'}

# СЧИТАЕМ ТО, ЧЕМ ТОРГУЮТ. В топ-125 CoinGecko входят токенизированные
# фонды и залоченные токены (FIGR_HELOC 23 млрд, BUIDL, USYC, JAAA, USTB…):
# рынка у них нет, «цена» — стоимость пая. TradingView их не считает, и с
# ними наш TOTAL был на 68 млрд (2.4%) больше, а USDT.D — на 0.15 п.п.
# меньше. Критерий без ручных списков: актив без рынка на Bybit входит в
# сумму, только если его суточный оборот не меньше MIN_VOLUME_PCT
# капитализации. Сверка 21.09.2026: USDT.D 6.412% при 6.425% у TradingView,
# BTC.D 59.39% при 59.49%, TOTAL 2.860 трлн при 2.854 — расхождение
# в сотых долях процента; при пороге 0 — 6.275%, при 2% — 6.481%.
MIN_VOLUME_PCT = 0.5


def tradable(coin, tick):
    """Есть рынок: пара на Bybit, стейблкоин или живой оборот."""
    if tick is not None or coin['symbol'] in STABLE:
        return True
    return float(coin.get('volume_pct') or 0) >= MIN_VOLUME_PCT


def compute(coins, tickers):
    """
    Капитализации по формуле TradingView. -> словарь показателей или None.

    coins — топ-монеты с предложением; tickers — цены спота Bybit. Монета
    без пары на Bybit берёт цену из часового снимка (её доля мала).
    """
    total = btc = eth = usdt = 0.0
    priced, up, counted, used = 0, 0, 0, 0
    for c in coins:
        sym = c['symbol']
        tick = tickers.get(sym + 'USDT')
        if not tradable(c, tick):
            continue
        used += 1
        if sym in STABLE:
            price = 1.0 if sym == 'USDT' else (tick['last'] if tick else c['price'])
        elif tick:
            price = tick['last']
            priced += 1
            counted += 1
            if tick['pct24'] > 0:
                up += 1
        else:
            price = c['price']
        cap = price * c['supply']
        total += cap
        if sym == 'BTC':
            btc = cap
        elif sym == 'ETH':
            eth = cap
        elif sym == 'USDT':
            usdt = cap
    if total <= 0 or btc <= 0 or usdt <= 0:
        return None
    return {'total': total, 'total2': total - btc, 'total3': total - btc - eth,
            'btc_d': btc / total * 100, 'usdt_d': usdt / total * 100,
            'usdt_cap': usdt, 'coins': used, 'priced_on_bybit': priced,
            'up_24h': up, 'counted_24h': counted}


# ── Сбор и ряд ───────────────────────────────────────────────────────────────

def collect(client, now=None):
    """Одна точка ряда: цены сейчас × предложение из кэша. -> запись или None."""
    now = now if now is not None else time.time()
    sup = supply(now)
    coins = sup.get('coins') or []
    if not coins or now - float(sup.get('at') or 0) > SUPPLY_MAX_AGE_SEC:
        log('   рынок в целом: нет свежего предложения монет — точка пропущена')
        return None
    tickers = _bybit_spot_tickers(client)
    row = compute(coins, tickers)
    if not row:
        return None
    row['ts'] = int(now * 1000)
    row['supply_at'] = int(float(sup['at']) * 1000)
    try:
        with open(store_path(), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row) + '\n')
    except OSError as exc:
        log(f'   рынок в целом: ряд не записан — {exc}')
    return row


def collect_if_due(client, now=None):
    """Раз в цикл; молчит при неудаче — блок в разметке станет прочерком."""
    global _last_collect
    now = now if now is not None else time.time()
    if now - _last_collect < COLLECT_EVERY_SEC:
        return None
    _last_collect = now
    try:
        return collect(client, now)
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ рынок в целом: сбор не удался — {exc}')
        return None


def series(upto=None, days=KEEP_DAYS):
    """Точки ряда за days суток до upto (мс), старые → новые."""
    upto = upto if upto is not None else int(time.time() * 1000)
    since = upto - days * 86_400_000
    out = []
    try:
        with open(store_path(), encoding='utf-8') as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ts = int(row.get('ts') or 0)
                if since <= ts <= upto:
                    out.append(row)
    except OSError:
        return []
    out.sort(key=lambda r: r['ts'])
    return out


def _at(rows, ts):
    """Последняя точка не позже ts."""
    best = None
    for r in rows:
        if r['ts'] <= ts:
            best = r
        else:
            break
    return best


def _streak(rows, key, upto):
    """
    Сколько суток подряд показатель растёт (>0) или падает (<0): по закрытиям
    суток UTC плюс текущее значение. 0 — направление сменилось только что.
    """
    day = 86_400_000
    today = upto - upto % day
    values = []
    for k in range(9, -1, -1):                        # закрытия прошлых суток, старые → новые
        point = _at(rows, today - k * day)
        if point is None:
            values = []                               # дыра в ряду — считаем от неё
            continue
        values.append(point[key])
    values.append(rows[-1][key])
    streak, sign = 0, 0
    for i in range(len(values) - 1, 0, -1):
        step = values[i] - values[i - 1]
        s = 1 if step > 0 else (-1 if step < 0 else 0)
        if s == 0 or (sign and s != sign):
            break
        sign = s
        streak += 1
    return streak * sign


def facts(upto=None):
    """
    Показатели для разметки: значения, изменения за 24ч/7д, полосы, ширина,
    возраст. None — ряда нет; stale=True — есть, но старый.
    """
    upto = upto if upto is not None else int(time.time() * 1000)
    rows = series(upto)
    if not rows:
        return None
    last = rows[-1]
    age_min = (upto - last['ts']) / 60_000
    out = {'usdt_d': last['usdt_d'], 'btc_d': last['btc_d'], 'total2': last['total2'],
           'age_min': round(age_min, 1),
           'supply_age_min': round((upto - int(last.get('supply_at') or last['ts'])) / 60_000, 1),
           'coins': last.get('coins'), 'priced_on_bybit': last.get('priced_on_bybit'),
           'usdt_cap': last.get('usdt_cap'),
           'up_24h': last.get('up_24h'), 'counted_24h': last.get('counted_24h'),
           'stale': age_min > STALE_MIN}
    for name, ms in (('24h', 86_400_000), ('7d', 7 * 86_400_000)):
        prev = _at(rows, last['ts'] - ms)
        if prev is None:
            continue
        out[f'usdt_d_{name}'] = last['usdt_d'] - prev['usdt_d']
        out[f'btc_d_{name}'] = last['btc_d'] - prev['btc_d']
        if prev.get('total2'):
            out[f'total2_{name}'] = (last['total2'] / prev['total2'] - 1) * 100
        # Ход капитализации USDT отдельно от доли: доля растёт и когда
        # печатают новые стейблы (деньги ПРИШЛИ на рынок), и когда падают
        # альты (деньги НИКУДА не пришли, просто подешевело остальное).
        # Это разные вещи, и по одной доле их не отличить.
        if prev.get('usdt_cap'):
            out[f'usdt_cap_{name}'] = (last.get('usdt_cap', 0) / prev['usdt_cap'] - 1) * 100
        if prev.get('total'):
            btc_prev = prev['total'] * prev['btc_d'] / 100
            btc_now = last['total'] * last['btc_d'] / 100
            out[f'btc_{name}'] = (btc_now / btc_prev - 1) * 100
    if len(rows) > 1:
        out['usdt_d_streak_days'] = _streak(rows, 'usdt_d', last['ts'])
    return out
