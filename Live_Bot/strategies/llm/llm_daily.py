"""
Анализ дня по активу — раз в сутки (решение владельца 15.10.2026).

ЧТО. Для каждого актива из списка на панели (настройка DAILY) бот раз в сутки
к часу DAILY.hour_utc (по умолчанию 06:00 UTC, перед Европой) присылает разбор
на торговый день по промту владельца: SMC/ICT, Вайкофф, Герчик; 1D задаёт
bias, 4H — местоположение, 15m — триггеры; 16 разделов, сценарии, сетапы с
инвалидацией и R:R. Полный текст — на панели (страница ИИ), выжимка — в
Telegram (событие asset_report).

ЧИСЛА СЧИТАЕТ КОД, ТЕКСТ ПИШЕТ МОДЕЛЬ. Модель путает уровни и арифметику
(опыты 2026-09, обзоры 28.09–10.10), поэтому всё, что можно посчитать, — в
ДАННЫХ: разметка, которую бот собирает для ИИ (llm_context.build: уровни с
касаниями и объёмом, свечи, участники, структура SMC, зоны, имбалансы,
ликвидность, профиль объёма, дельта, ликвидации, рынок в целом и USDT.D), и
свой блок по таймфреймам промта (mtf_block: свинги 1D, слом и диапазон 4H с
серединой и premium/discount, сутки на 15m, снятие экстремумов прошлого дня).
Модель обязана брать уровни только оттуда; R:R по её сетапам пересчитывает код.

ЧЕСТНО О ЦЕННОСТИ. Это аналитика, не проверенный сигнал: прогнозы модели на
истории и вживую не лучше монетки (docs/ИИ_замечания_на_проверку.md). Поэтому
каждый отчёт пишется на диск с оценками и сетапами (JSON в конце ответа) —
сбывались ли они, можно проверить потом.

МОДЕЛЬ ОДНА. Данные собираются в цикле бота (клиент биржи), вопрос — в своём
потоке, когда сервер модели свободен (/slots): ответ ~30 минут, и тетрадь,
которая спрашивает модель сразу после закрытия часа, не должна его ждать.
Активы — по одному в час, последний к часу отправки. Модель не освободилась к
LATEST_START — разбор переносится на следующий час.

Настройки (DAILY) приходят от цикла бота: стратегический слой не читает слой
счетов (tests/test_module_layers.py). Умолчания и проверка полей — здесь, их
берёт accounts/settings_store.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from infra import config
from infra.logger import log

DIR = 'daily_reports'
START_WINDOW = (5, 25)        # минуты часа, когда собираются данные (тетрадь — в начале часа)
LATEST_START = 32             # позже — вопрос в следующий час: ответ ~30 мин, к началу часа модель нужна тетради
TIMEOUT_S = 50 * 60
MAX_TOKENS = 3300             # 16 разделов по-русски ~ 10–12 тыс. знаков
MIN_TOKENS = 1800             # меньше места в окне — ответ обрежется: данные ужимаются
CTX_FALLBACK = 12288
MAX_PAIRS = 6                 # по одному в час: шесть — уже с полуночи
MAX_ATTEMPTS = 2              # сбой модели — ещё одна попытка в следующий час

# Пул активов на выбор — пары, по которым бот собирает всё: свечи, открытый
# интерес, фандинг, лонг/шорт, ликвидации, ленту (config.TRADING_PAIRS_POOL).
ALLOWED = tuple(config.TRADING_PAIRS_POOL)
DEFAULTS = {'enabled': True, 'pairs': ('BTCUSDT', 'ETHUSDT'), 'hour_utc': 6}

SYSTEM = """Ты — профессиональный крипто-трейдер-аналитик. Методология: Smart Money Concepts / ICT (BOS, CHoCH, Order Block, FVG, external/internal liquidity, equal highs/lows, premium/discount, displacement, IDM), Вайкофф (фазы, accumulation/re-accumulation/distribution, Spring/Upthrust, absorption, acceptance) и Герчик (торговля от уровней, ложные пробои, стоп за точкой инвалидации идеи).

Вместо скриншотов тебе даны ДАННЫЕ, посчитанные ботом по бирже Bybit: таймфреймы 1D (HTF), 4H (MTF), 15m (LTF) и часовой (уровни, зоны, профиль объёма, дельта, свечи), открытый интерес, фандинг, лонг/шорт, ликвидации, стакан, USDT.D и рынок в целом.

ПРАВИЛА АНАЛИЗА:
1. Строго сверху вниз: 1D задаёт bias, 4H — местоположение цены (тренд/рендж), 15m — триггеры и ловушки.
2. Используй ТОЛЬКО ДАННЫЕ. Все уровни и числа бери из них — не придумывай своих. Где блок пуст или стоит прочерк, пиши «данных нет» и снижай уверенность. В начале перечисли, какие данные есть и каких нет.
3. Диапазон, его середина (equilibrium) и зоны premium/discount уже посчитаны в ДАННЫХ — используй их.
4. Ликвидность — магнит: равные экстремумы, многократные свинги, экстремумы дня и недели. Определи, какой пул цена вероятнее заберёт первым.
5. Вход — только после подтверждения: sweep → быстрый reclaim (1–3 свечи) → CHoCH на 15m, либо breakout → закрытие 4H за уровнем → ретест с удержанием (acceptance). Прокол без закрепления = ловушка.
6. Инвалидация — уровень, где ломается структура идеи, а не «комфортный» стоп.
7. Для каждого сетапа укажи R:R до каждой цели; R:R < 1.5 до первой цели — слабый сетап.
8. Вероятности сценариев — из скоринга (Bullish + Bearish = 10), смещай к 50/50 пропорционально недостающим данным, кратко объясни.
9. Все уровни — конкретными числами из ДАННЫХ.

ФОРМАТ ОТВЕТА (строго эти разделы и в этом порядке):
Актив и данные — что проанализировано и каких данных нет.
1) MARKET REGIME — HTF и LTF режим (markup/markdown/accumulation/distribution/range/compression), поведение LTF.
2) HTF STRUCTURE — 1D: HH/HL или LH/LL, ключевые свинги. 4H: последний BOS, диапазон, середина.
3) EMOTIONAL STATE — fear/greed по поведению цены, признаки FOMO или капитуляции (Upthrust/Spring).
4) POSITIONING — OI, funding, лонг/шорт, дельта, ликвидации; USDT.D.
5) LIQUIDITY MAP — external (выше/ниже, уровни) и internal (внутри диапазона).
6) ALGO BEHAVIOR — «сбор ликвидности → возврат/продолжение», absorption, acceptance, куда цена вероятнее пойдёт сначала.
7) NARRATIVE CONTEXT — новости (если их нет в ДАННЫХ — так и написать).
8) CONTINUATION VS EXHAUSTION — отдельно для HTF и MTF/LTF.
9) BULLISH SCENARIO — логика, триггеры, цели ликвидности, вход (breakout-retest и/или sweep-reclaim) с уровнями, invalidation, R:R; основной или второстепенный.
10) BEARISH SCENARIO — то же; основной или тактический.
11) INVALIDATION LEVELS — где ломается HTF bias и при каком условии он меняется.
12) HIGH-PROBABILITY LIQUIDITY TARGETS — верх и низ по очерёдности.
13) SCORING — Bullish X/10, Bearish Y/10 (X+Y=10), Trap Risk Z/10 — с обоснованием.
14) EXECUTION QUALITY — обязательные подтверждения и как нехватка данных влияет на уверенность.
15) RISK CONDITIONS — полный/уменьшенный размер и почему, откуда торговать лонги и шорты, правило стопа, частичная фиксация.
16) FINAL PROBABILISTIC CONCLUSION — контекст в 1–2 предложениях; вероятности сценариев в %; 2–3 лучших сетапа: Название: условие входа → триггер → цели → инвалидация → R:R. Роль USDT.D и ограничения анализа.

Пиши на русском, термины SMC/Вайкофф оставляй на английском. Без воды, только уровни, условия и логика.
ПОСЛЕДНЕЙ строкой — JSON одной строкой (для журнала, без пояснений):
{"bull": X, "bear": Y, "trap": Z, "p_bull": проценты, "p_bear": проценты, "setups": [{"name": "...", "side": "long|short", "entry": число, "stop": число, "targets": [число, число]}]}"""

_job = {'thread': None, 'pair': None}
_lock = threading.Lock()


# ── Настройки ───────────────────────────────────────────────────────────────

def defaults():
    return {'enabled': DEFAULTS['enabled'], 'pairs': list(DEFAULTS['pairs']), 'hour_utc': DEFAULTS['hour_utc']}


def clean(item, current=None):
    """Правка раздела DAILY поверх текущего — с проверкой: пары только из пула, без
    повторов, не больше MAX_PAIRS; час — целый 0–23. Негодное поле не меняется."""
    out = dict(current) if isinstance(current, dict) else defaults()
    item = item if isinstance(item, dict) else {}
    if 'enabled' in item:
        out['enabled'] = bool(item['enabled'])
    if isinstance(item.get('pairs'), (list, tuple)):
        chosen = []
        for pair in item['pairs']:
            pair = str(pair).upper().strip()
            if pair in ALLOWED and pair not in chosen:
                chosen.append(pair)
        out['pairs'] = chosen[:MAX_PAIRS]
    if 'hour_utc' in item and not isinstance(item['hour_utc'], bool):
        try:
            hour = int(item['hour_utc'])
            if 0 <= hour <= 23 and float(item['hour_utc']) == hour:
                out['hour_utc'] = hour
        except (TypeError, ValueError):
            pass
    return out


def report_dir():
    return os.path.join(config.DATA_DIR, DIR)


def report_path(day, pair):
    return os.path.join(report_dir(), f'{day}_{pair}.json')


def slot_hours(pairs, hour_utc):
    """Час начала разбора каждого актива: по одному в час, последний — за час до отправки."""
    n = len(pairs)
    return {p: max(0, int(hour_utc) - (n - i)) for i, p in enumerate(pairs)}


# ── Блок таймфреймов промта (считает код) ───────────────────────────────────

def pivots(h, l, k=2):
    """Свинги-фракталы: строго выше/ниже k баров слева, не ниже/не выше k справа."""
    highs, lows = [], []
    for j in range(k, len(h) - k):
        if all(h[m] < h[j] for m in range(j - k, j)) and all(h[m] <= h[j] for m in range(j + 1, j + k + 1)):
            highs.append(j)
        if all(l[m] > l[j] for m in range(j - k, j)) and all(l[m] >= l[j] for m in range(j + 1, j + k + 1)):
            lows.append(j)
    return highs, lows


def swing_labels(h, l, k=2, last=4):
    """Последние свинги с метками HH/LH и HL/LL и итоговая структура."""
    highs, lows = pivots(h, l, k)
    hs = [(j, h[j], ('HH' if h[j] > h[highs[i - 1]] else 'LH') if i else '—') for i, j in enumerate(highs)][-last:]
    ls = [(j, l[j], ('HL' if l[j] > l[lows[i - 1]] else 'LL') if i else '—') for i, j in enumerate(lows)][-last:]
    trend = 'не определена'
    if hs and ls:
        if hs[-1][2] == 'HH' and ls[-1][2] == 'HL':
            trend = 'восходящая (HH + HL)'
        elif hs[-1][2] == 'LH' and ls[-1][2] == 'LL':
            trend = 'нисходящая (LH + LL)'
        else:
            trend = f'смешанная ({hs[-1][2]} + {ls[-1][2]})'
    return hs, ls, trend


def last_break(h, l, c, k=2):
    """Последний слом по закрытию за подтверждённым свингом: (бар, 'BOS'|'CHoCH', сторона, уровень) или None.
    Свинг известен через k баров после вершины; один свинг ломается один раз."""
    highs, lows = pivots(h, l, k)
    known_h = {j + k: j for j in highs}
    known_l = {j + k: j for j in lows}
    hp = lp = None
    used_h = used_l = True
    trend = 0
    out = None
    for i in range(len(c)):
        if i in known_h:
            hp, used_h = h[known_h[i]], False
        if i in known_l:
            lp, used_l = l[known_l[i]], False
        if hp is not None and not used_h and c[i] > hp:
            out = (i, 'BOS' if trend == 1 else 'CHoCH', 'вверх', hp)
            trend, used_h = 1, True
        elif lp is not None and not used_l and c[i] < lp:
            out = (i, 'BOS' if trend == -1 else 'CHoCH', 'вниз', lp)
            trend, used_l = -1, True
    return out


def _rows(df):
    return (df['high'].astype(float).tolist(), df['low'].astype(float).tolist(),
            df['close'].astype(float).tolist())


def _f(x):
    return f'{x:.6g}'


def mtf_block(d1, h4, m15, price, macro=None):
    """Факты по таймфреймам промта — только закрытые свечи. Каждый кусок — или числа, или «данных нет»."""
    out = ['ТАЙМФРЕЙМЫ (посчитано ботом по закрытым свечам; свинг — фрактал 2+2)']
    try:
        h, l, c = _rows(d1)
        hs, ls, trend = swing_labels(h, l)
        out.append(f'1D: структура {trend}. Свинг-максимумы: '
                   + (', '.join(f'{_f(p)} ({lab})' for _, p, lab in hs) or 'нет')
                   + '. Свинг-минимумы: ' + (', '.join(f'{_f(p)} ({lab})' for _, p, lab in ls) or 'нет') + '.')
        br = last_break(h, l, c)
        if br:
            out.append(f'1D: последний слом — {br[1]} {br[2]} за {_f(br[3])}, {len(c) - 1 - br[0]} дн. назад.')
        out.append(f'1D: вчера — максимум {_f(h[-1])}, минимум {_f(l[-1])}, закрытие {_f(c[-1])}; '
                   f'за 7 дней — максимум {_f(max(h[-7:]))}, минимум {_f(min(l[-7:]))}.')
    except Exception:                                  # noqa: BLE001
        out.append('1D: данных нет.')
    try:
        h, l, c = _rows(h4)
        hs, ls, trend = swing_labels(h, l)
        out.append(f'4H: структура {trend}.')
        br = last_break(h, l, c)
        if br:
            out.append(f'4H: последний слом — {br[1]} {br[2]} за {_f(br[3])}, {len(c) - 1 - br[0]} баров назад.')
        if hs and ls and hs[-1][1] > ls[-1][1]:
            top, bot = hs[-1][1], ls[-1][1]
            eq = (top + bot) / 2
            pos = (price - bot) / (top - bot) * 100
            where = ('выше диапазона' if price > top else 'ниже диапазона' if price < bot
                     else 'premium' if price > eq else 'discount')
            out.append(f'4H диапазон (последние свинги): верх {_f(top)}, низ {_f(bot)}, '
                       f'equilibrium {_f(eq)}; цена {_f(price)} — {pos:.0f}% от низа ({where}).')
    except Exception:                                  # noqa: BLE001
        out.append('4H: данных нет.')
    try:
        h, l, c = _rows(m15)
        h, l, c = h[-200:], l[-200:], c[-200:]
        line = f'15m: за сутки максимум {_f(max(h[-96:]))}, минимум {_f(min(l[-96:]))}'
        br = last_break(h, l, c)
        if br:
            line += f'; последний слом — {br[1]} {br[2]} за {_f(br[3])}, {len(c) - 1 - br[0]} баров назад'
        out.append(line + '.')
        dh, dl, _ = _rows(d1)
        pdh, pdl = dh[-1], dl[-1]
        recent = range(max(0, len(h) - 96), len(h))
        took_h = any(h[i] > pdh and min(c[i:i + 4]) < pdh for i in recent)
        took_l = any(l[i] < pdl and max(c[i:i + 4]) > pdl for i in recent)
        out.append(f'15m: экстремумы вчерашнего дня {_f(pdh)} / {_f(pdl)} — за сутки '
                   f"{'максимум снят с возвратом в 4 свечи (sweep)' if took_h else 'максимум не снят с возвратом'}, "
                   f"{'минимум снят с возвратом в 4 свечи (sweep)' if took_l else 'минимум не снят с возвратом'}.")
    except Exception:                                  # noqa: BLE001
        out.append('15m: данных нет.')
    m = macro or {}
    if m.get('usdt_d') is not None and not m.get('stale'):
        d24, d7 = m.get('usdt_d_24h'), m.get('usdt_d_7d')
        out.append(f"USDT.D: {m['usdt_d']:.2f}%"
                   + (f', за 24ч {d24:+.2f} п.п.' if d24 is not None else '')
                   + (f', за 7д {d7:+.2f} п.п.' if d7 is not None else '')
                   + ' (рост — риск-офф, деньги уходят из крипты в стейблкоин); USDT.D 4H: данных нет.')
    else:
        out.append('USDT.D: данных нет.')
    return '\n'.join(out)


def mood_text(mood):
    """Настроение рынка из общего слоя (DVOL, премия Coinbase, доля спота) — по-русски; нет — «данных нет»."""
    if not mood:
        return 'НАСТРОЕНИЕ РЫНКА: данных нет.'

    def v(x, fmt):
        return '—' if x is None else format(float(x), fmt)
    share = mood.get('btc_spot_share_24h')
    return (f"НАСТРОЕНИЕ РЫНКА: страх в опционах BTC (DVOL) {v(mood.get('dvol'), '.1f')}, "
            f"за сутки {v(mood.get('dvol_chg_24h'), '+.1f')}%, ранг за 30 дней {v(mood.get('dvol_pct_30d'), '.2f')} "
            f"(1 = больше всего страха за месяц); премия Coinbase к Binance по BTC {v(mood.get('btc_cb_prem_bp'), '+.1f')} б.п. "
            f"(минус — американцы продают); доля спота в обороте BTC "
            f"{'—' if share is None else f'{share * 100:.0f}%'}.")


# Строки разметки, которые задают правила ДРУГОЙ задачи (планов ИИ-стратегии):
# какие уровни «годятся» под её вход и стоп, её минимальный стоп, её счёт.
# В разборе дня они навязывали бы чужие правила — разбор идёт по методике
# владельца, а не по правилам торговой ИИ.
_RULE_LINE = re.compile(r'^(Минимальный стоп:|  ГОДЯТСЯ ПОД ВХОД:)')
_STOP_TAG = re.compile(r'\s+← СТОП: \S+')
_STOP_HEAD = '«← СТОП» — стоп можно только за такой уровень; '


def context_text(text):
    """Разметка ИИ без правил торговой ИИ-стратегии и её счёта."""
    out = []
    for line in (text or '').split('\n'):
        if line.startswith('ТВОЙ СЧЁТ'):
            break
        if _RULE_LINE.match(line):
            continue
        out.append(_STOP_TAG.sub('', line).replace(_STOP_HEAD, ''))
    return '\n'.join(out).rstrip()


# ── Вопрос, ответ, журнал ───────────────────────────────────────────────────

def collect(pair, client=None, now=None):
    """Данные актива — В ЦИКЛЕ бота (клиент биржи): разметка ИИ + блок таймфреймов.
    -> (текст вопроса, цена) или (None, причина)."""
    from analysis import market as market_data
    from analysis import llm_market
    from strategies.llm import llm_context
    h1 = market_data.fetch_ohlcv('1h', limit=500, symbol=pair, client=client)
    if h1 is None or len(h1) < 200:
        return None, 'мало часовых свечей'
    d1 = market_data.fetch_ohlcv('1d', limit=120, symbol=pair, client=client)
    h4 = market_data.fetch_ohlcv('4h', limit=300, symbol=pair, client=client)
    m15 = market_data.fetch_ohlcv('15m', limit=300, symbol=pair, client=client)
    btc = market_data.fetch_ohlcv('1h', limit=500, symbol='BTCUSDT', client=client) if pair != 'BTCUSDT' else None
    snapshot = llm_market.snapshot(pair, h1, client=client, benchmark=btc)
    ctx = llm_context.build(pair, h1, market=snapshot)
    price = float(h1['close'].iloc[-1])

    def closed(df):
        # Последняя свеча у биржи — формирующаяся: структура — по закрытым.
        return df.iloc[:-1] if df is not None and len(df) > 3 else df
    mtf = mtf_block(closed(d1), closed(h4), closed(m15), price, (snapshot or {}).get('macro'))
    try:
        mood = market_data.mood()
    except Exception:                                  # noqa: BLE001
        mood = {}
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y-%m-%d %H:%M')
    text = (f'АКТИВ: {pair}, биржа Bybit. Сейчас {stamp} UTC, цена {price:.6g}.\n\n{mtf}\n\n{mood_text(mood)}\n\n'
            'ЧАСОВОЙ ГРАФИК, УРОВНИ И УЧАСТНИКИ\n' + (context_text(ctx.get('text')) or 'данных нет')
            + '\n\nНОВОСТИ: данных нет.')
    return text, price


_CANDLES_HEAD = re.compile(r'^ПОСЛЕДНИЕ \d+ ЧАСОВЫХ СВЕЧЕЙ')


def _cut_candles(text):
    """Ужать вопрос, если ответу не хватает окна: уходят свечи часового графика (llm_context)."""
    lines = text.split('\n')
    try:
        start = next(i for i, x in enumerate(lines) if _CANDLES_HEAD.match(x))
    except StopIteration:
        return None
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return '\n'.join(lines[:start] + ['СВЕЧИ ЧАСОВОГО ГРАФИКА: опущены — не хватило окна модели.'] + lines[end:])


def _json_span(text):
    """Служебный JSON ответа — последний объект с ключом "setups" (сетапы в нём —
    вложенные объекты, регуляркой не взять): (начало, конец, объект) или None."""
    text = text or ''
    decoder = json.JSONDecoder()
    pos = text.rfind('"setups"')
    while pos >= 0:
        start = text.rfind('{', 0, pos)
        while start >= 0:
            try:
                obj, end = decoder.raw_decode(text, start)
            except ValueError:
                obj, end = None, 0
            if isinstance(obj, dict) and 'setups' in obj and end > pos:
                return start, end, obj
            start = text.rfind('{', 0, start)
        pos = text.rfind('"setups"', 0, pos)
    return None


def parse(text):
    """JSON-строка в конце ответа -> словарь или None."""
    span = _json_span(text)
    return span[2] if span else None


def strip_json(text):
    """Ответ без служебного JSON (и пустой обёртки ```json, если модель её поставила)."""
    span = _json_span(text)
    if span:
        text = text[:span[0]] + text[span[1]:]
    return re.sub(r'```(?:json)?\s*```', '', text or '').rstrip()


def rr_table(parsed):
    """R:R сетапов модели — пересчёт кодом:
    [{'name', 'side', 'entry', 'stop', 'targets', 'rr', 'weak', 'why'}]."""
    out = []
    for s in (parsed or {}).get('setups') or []:
        try:
            entry, stop = float(s['entry']), float(s['stop'])
            targets = [float(t) for t in s.get('targets') or []]
        except (KeyError, TypeError, ValueError):
            continue
        side = 1 if str(s.get('side', '')).lower().startswith('l') else -1
        row = {'name': str(s.get('name') or '—')[:80], 'side': 'long' if side == 1 else 'short',
               'entry': entry, 'stop': stop, 'targets': targets, 'rr': [], 'weak': True, 'why': ''}
        risk = (entry - stop) * side
        if risk <= 0:
            row['why'] = 'стоп не по ту сторону входа'
        else:
            row['rr'] = [round((t - entry) * side / risk, 2) for t in targets]
            if any(r <= 0 for r in row['rr']):
                row['why'] = 'цель не по ту сторону входа'
            row['weak'] = not row['rr'] or row['rr'][0] < 1.5
        out.append(row)
    return out


def _raw(text):
    return (f'<|im_start|>system\n{SYSTEM}<|im_end|>\n<|im_start|>user\n{text}<|im_end|>\n'
            f'<|im_start|>assistant\n<think>\n\n</think>\n\n')


def fit(question, count=None, ctx=None):
    """Сколько токенов дать ответу: окно минус вопрос. Не хватает MIN_TOKENS — вопрос
    без свечей часового графика. -> (вопрос, n_predict). Счёт недоступен — как есть."""
    from infra import llm_server
    count = count or (lambda t: len(llm_server._tokenize(t)))
    if ctx is None:
        ctx = ((llm_server.props().get('default_generation_settings') or {}).get('n_ctx') or CTX_FALLBACK)
    try:
        room = int(ctx) - count(_raw(question)) - 64
    except Exception:                                  # noqa: BLE001
        return question, MAX_TOKENS
    if room < MIN_TOKENS:
        shorter = _cut_candles(question)
        if shorter:
            question = shorter
            room = int(ctx) - count(_raw(question)) - 64
    if room < MIN_TOKENS:
        raise RuntimeError(f'вопрос не помещается в окно модели: на ответ остаётся {room} токенов')
    return question, min(MAX_TOKENS, room)


def ask_model(question):
    """Вопрос серверу модели без мысли. -> (ответ, секунды, вопрос как ушёл)."""
    from infra import llm_server
    question, n_predict = fit(question)
    started = time.time()
    out = llm_server._completion({'prompt': _raw(question), 'n_predict': n_predict, 'temperature': 0.3,
                                  'top_k': 20, 'top_p': 0.9, 'stop': ['<|im_end|>'], 'cache_prompt': True},
                                 TIMEOUT_S)
    return out.get('content') or '', round(time.time() - started, 1), question


def _esc(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def conclusion(answer):
    """Раздел 16 без JSON-строки; нет раздела — хвост ответа."""
    answer = strip_json(answer).strip()
    m = re.search(r'16\)\s*\**\s*FINAL[^\n]*\n?(.*)', answer, re.S | re.I)
    return (m.group(1) if m else answer[-1500:]).strip()


def summary(record):
    """Выжимка для Telegram (HTML, < 4096 знаков): вывод, скоринг, сетапы с R:R кода."""
    head = f"🗓 <b>Анализ дня · {_esc(record['pair'].replace('USDT', ''))}</b> · цена {record.get('price', 0):.6g}"
    lines = [head, '━━━━━━━━━━━━━━━━━━━━', _esc(conclusion(record.get('answer'))[:2400])]
    p = record.get('parsed') or {}
    if p:
        lines.append(f"\nСкоринг: bull {_esc(p.get('bull', '—'))}/10 · bear {_esc(p.get('bear', '—'))}/10 · "
                     f"ловушка {_esc(p.get('trap', '—'))}/10")
    for s in record.get('rr') or []:
        rr = ' / '.join(f'{x:g}' for x in s['rr']) or '—'
        lines.append(f"{'🟢' if s['side'] == 'long' else '🔴'} {_esc(s['name'])}: вход {s['entry']:g}, "
                     f"стоп {s['stop']:g}, R:R {rr}" + (' — слабый' if s['weak'] else '')
                     + (f" ({s['why']})" if s['why'] else ''))
    lines.append('\n<i>Разбор модели, не сигнал: R:R пересчитан кодом. Все 16 разделов — на панели, страница ИИ.</i>')
    return '\n'.join(lines)[:4000]


def _save(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _now():
    return datetime.now(timezone.utc)


def run(pair, day, question, price, ask=ask_model, idle=None, notify=True, clock=None, pause=time.sleep):
    """Поток разбора: ждёт свободную модель (не позже LATEST_START минуты), спрашивает,
    пишет отчёт, шлёт выжимку. Модель так и не освободилась — отчёта нет, следующий час."""
    from strategies.llm import llm_shadow
    idle = idle or llm_shadow.server_idle
    clock = clock or _now
    path = report_path(day, pair)
    try:
        while not idle():
            if clock().minute >= LATEST_START:
                log(f'   ИИ: анализ дня {pair} — модель занята, перенос на следующий час')
                return None
            pause(30)
        text, seconds, sent = ask(question)
        if not text.strip():
            raise RuntimeError('пустой ответ модели')
        parsed = parse(text)
        record = {'day': day, 'pair': pair, 'price': price, 'built_at': clock().isoformat(timespec='seconds'),
                  'seconds': seconds, 'question': sent, 'answer': text, 'parsed': parsed, 'rr': rr_table(parsed)}
        _save(path, record)
        log(f'   ИИ: анализ дня {pair} готов за {seconds:.0f} с')
        if notify:
            try:
                from infra.outbox import telegram
                telegram.asset_report(summary(record))
            except Exception as exc:                   # noqa: BLE001
                log(f'   ИИ: анализ дня {pair} не отправлен в Telegram ({exc})')
        return record
    except Exception as exc:                           # noqa: BLE001
        log(f'   ИИ: анализ дня {pair} не получен ({exc})')
        attempts = ((_load(path) or {}).get('attempts') or 0) + 1
        _save(path, {'day': day, 'pair': pair, 'error': str(exc)[:300], 'attempts': attempts,
                     'built_at': clock().isoformat(timespec='seconds')})
        return None
    finally:
        with _lock:
            _job['pair'] = None


def due(cfg, now=None):
    """Какой актив разбирать сейчас: (пара, день) или None."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg.get('enabled') or not cfg.get('pairs') or not getattr(config, 'LLM_SERVER_URL', ''):
        return None
    now = now or _now()
    if not START_WINDOW[0] <= now.minute < START_WINDOW[1]:
        return None
    day = now.strftime('%Y-%m-%d')
    slots = slot_hours(list(cfg['pairs'])[:MAX_PAIRS], cfg.get('hour_utc', DEFAULTS['hour_utc']))
    for pair, hour in slots.items():
        if now.hour < hour:
            continue
        done = _load(report_path(day, pair))
        if done is None or (done.get('error') and (done.get('attempts') or 1) < MAX_ATTEMPTS):
            return pair, day
    return None


def tick(client=None, cfg=None, now=None):
    """Раз в цикл бота: пора — собрать данные (здесь, с клиентом биржи) и отдать модели в поток."""
    with _lock:
        if _job['pair'] is not None:
            return False
    job = due(cfg, now)
    if not job:
        return False
    pair, day = job
    path = report_path(day, pair)
    try:
        question, price = collect(pair, client=client, now=now)
    except Exception as exc:                           # noqa: BLE001
        question, price = None, f'данные не собраны: {exc}'
    if question is None:
        log(f'   ИИ: анализ дня {pair} — {price}')
        attempts = ((_load(path) or {}).get('attempts') or 0) + 1
        _save(path, {'day': day, 'pair': pair, 'error': str(price)[:300], 'attempts': attempts,
                     'built_at': (now or _now()).isoformat(timespec='seconds')})
        return False
    with _lock:
        _job['pair'] = pair
        _job['thread'] = threading.Thread(target=run, args=(pair, day, question, price),
                                          name='llm-daily', daemon=True)
        _job['thread'].start()
    log(f'   ИИ: анализ дня {pair} — данные собраны, вопрос ждёт модель')
    return True


def busy():
    with _lock:
        return _job['pair']


def recent(days=7):
    """Отчёты за последние дни для панели, новые сверху: без текста вопроса, ответ —
    без служебной JSON-строки, вывод (раздел 16) — отдельным полем."""
    folder = report_dir()
    if not os.path.isdir(folder):
        return []
    out, seen = [], set()
    for name in sorted((n for n in os.listdir(folder) if n.endswith('.json')), reverse=True):
        rec = _load(os.path.join(folder, name))
        if not rec or 'day' not in rec:
            continue
        if rec['day'] not in seen and len(seen) >= days:
            break
        seen.add(rec['day'])
        rec.pop('question', None)
        if rec.get('answer'):
            rec['conclusion'] = conclusion(rec['answer'])
            rec['answer'] = strip_json(rec['answer'])
        out.append(rec)
    return out


def export_text():
    return ('Промт ИИ «анализ дня по активу» — экспорт из Live_Bot/strategies/llm/llm_daily.py.\n'
            'Промт меняется только вместе с этим файлом: совпадение держит tests/test_llm_daily.py.\n'
            'Пересобрать (из Live_Bot): python -m strategies.llm.llm_daily export\n\n'
            f'Модель без мысли, n_predict до {MAX_TOKENS} (сколько оставляет окно), temperature 0.3.\n'
            'Вопрос: АКТИВ + ТАЙМФРЕЙМЫ (mtf_block: 1D, 4H, 15m, USDT.D) + НАСТРОЕНИЕ РЫНКА + '
            'разметка ИИ (llm_context.build без правил торговой ИИ) + НОВОСТИ.\n\n'
            f'=== СИСТЕМА ===\n{SYSTEM}\n')


EXPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))), 'docs', 'Промт_ИИ_анализ_дня.txt')


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['export']:
        with open(EXPORT, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(export_text())
        print(EXPORT)
