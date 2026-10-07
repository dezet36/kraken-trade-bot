"""
ИИ-трейдер по своей тетради (LLM_MODE=notebook, 27.09.2026).

Модель торгует сама — по закономерностям, которые нашла и проверила на
истории (docs/ИИ_замечания_на_проверку.md, п. 68–69):
    P1  отскок после широкой разгрузки — ≥ 4 из 20 монет за 3 ч упали > 4.3%
        за 4 ч при падении ОИ > 4.7%; лонг 24 ч, стоп 1.0 суточного размаха;
    P2  накопление при расхождении с BTC — монета сильнее BTC на 2.5%+ за
        сутки при падении BTC на 1%+, ОИ +4% за сутки, объём > 1.5 обычного;
        лонг 48 ч, стоп 1.3 суточного размаха (правило модели, круг 3);
    B3  раздача на отскоке рынка (тетрадь v2, 28.09.2026) — BTC за 30 дней вниз,
        за сутки вверх на 1%+, монета слабее BTC на 2.5%+ при росте ОИ и объёма;
        шорт 48 ч, стоп 1.3 суточного размаха.
Раз в час, на закрытии, код ищет НОВЫЕ сигналы (первый час серии) по парам
пула; есть — модель видит тетрадь, сводку по рынку и сигналам, что уже держит
и сколько мест свободно (всего SLOTS), и решает, что взять. Вопрос уходит в
свой поток, ответ забирает ближайший цикл: общий цикл бота модель не ждёт.
Вход — сразу по рынку по цене биржи на момент входа, выход — по стопу или по
сроку закономерности. Раз в 4 часа модель пишет обзор рынка для владельца
(журнал, Telegram); каждое решение пишется в llm_notebook_log.jsonl вместе с
выбором механики — для замера её вклада.

Тетрадь, вопрос и исполнение — те же, что в историческом прогоне
research/ai_model_trader_bt.py (совпадение текста держит тест). Данные и
признаки — общий слой (flow_data, flow_features); правила решений — свои, здесь
(CLAUDE.md, «Изоляция стратегий»).
"""
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np
import pandas as pd

import config
from logger import log

NAME = 'LLM'
SLOTS = 6
HOUR_MS = 3_600_000


def enabled():
    return str(getattr(config, 'LLM_MODE', 'plans')).strip().lower() == 'notebook'


# Исполнение — своё (strategy_profile читает его в этом режиме): вход по рынку
# сразу (заявка за рынком исполняется тейкером), без кулдауна — как в замере,
# выход по сроку закономерности (у позиции свой срок; здесь — запасной).
EXECUTION = SimpleNamespace(
    PENDING_ORDER_MAX_HOURS=1.0,
    COOLDOWN_HOURS=0.0,
    MAX_ENTRY_COST_SHARE_PCT=10.0,
    MAX_POSITION_HOLD_HOURS=48.0,
    CANCEL_PENDING_AT_TARGET=False,
    FILL_THROUGH_MARKET=True,
    MIN_SL_PCT=0.0,
)

POOL = ('AAVEUSDT', 'ADAUSDT', 'ARBUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT', 'COTIUSDT',
        'DOGEUSDT', 'DOTUSDT', 'ETHUSDT', 'LINKUSDT', 'LTCUSDT', 'NEARUSDT', 'SHIB1000USDT',
        'SOLUSDT', 'SUIUSDT', 'UNIUSDT', 'XLMUSDT', 'XRPUSDT', 'ZECUSDT')

# Тетрадь v3 (28.09.2026): P1 и P2 перенесены на 10 пар сверх базовых 20 (research/ai_extra_pairs.py:
# на новых парах R > 0 и в train, и в valid). Широта разгрузки (P1) считается только по базовым 20,
# B3 на новых парах не подтвердилась — торгуется только на базовых.
EXTRA = ('BCHUSDT', 'ETCUSDT', 'ATOMUSDT', 'FILUSDT', 'TRXUSDT', 'OPUSDT', 'APTUSDT', 'INJUSDT',
         '1000PEPEUSDT', 'SEIUSDT')
# 30.09.2026 (docs п. 80, research/ai_universe_extend.py): ещё 12 монет, выбраны по бирже (листинг до
# 30.06.2023, медиана оборота Bybit ≥ $3 млн). На них перенеслась только P1: train +0.184R, valid +0.451R,
# 2021 +0.601R; P4 — нет (train −0.02R), B3 — только основные. Широта каскада — по-прежнему по основным 20.
WIDE = ('XMRUSDT', 'DASHUSDT', 'HBARUSDT', 'CRVUSDT', 'ICPUSDT', 'LDOUSDT', 'ALGOUSDT', 'STXUSDT', 'GALAUSDT',
        'ZENUSDT', 'EGLDUSDT', 'SANDUSDT')
ON_WIDE = ('P1',)
UNIVERSE = POOL + EXTRA + WIDE

# Выключено владельцем 30.09.2026 (docs п. 75): P2 вне выборки ≈ 0 (2021 −0.05R, test ≈ +0.05…0.09R)
# при половине всех сделок. С тетради v3.4 (п. 77) её нет и в тексте; запись в PATTERNS оставлена ради
# стенда и прежних проверок (research/ai_model_trader_bt.PATTERNS).
OFF = ('P2',)

PATTERNS = {
    'P1': {'label': 'CASCADE BOUNCE', 'side': 'long', 'hold': 24, 'stop': 1.0, 'rank': ('ret_4h', 1),
           'conditions': [['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381], ['cascade_count_3h', '>=', 4]]},
    'P2': {'label': 'BTC DIVERGENCE ACCUMULATION', 'side': 'long', 'hold': 48, 'stop': 1.3, 'rank': ('rel_24h', -1),
           'conditions': [['rel_24h', '>=', 2.5], ['btc_ret_24h', '<=', -1.0], ['oi_chg_24h', '>=', 4.0],
                          ['vol_z', '>=', 1.5]]},
    'B3': {'label': 'DISTRIBUTION ON MARKET BOUNCE', 'side': 'short', 'hold': 48, 'stop': 1.3, 'rank': ('rel_24h', 1),
           'universe': 'core',
           'conditions': [['btc_ret_30d', '<', 0.0], ['rel_24h', '<=', -2.5], ['btc_ret_24h', '>=', 1.0],
                          ['oi_chg_24h', '>=', 4.0], ['vol_z', '>=', 1.5]]},
    # Тетрадь v3.4 (30.09.2026, п. 76–77): правило круга 1 модели, прошло train, valid и 2021; все 30 пар.
    'P4': {'label': 'SHORT SQUEEZE', 'side': 'long', 'hold': 48, 'stop': 1.5, 'rank': ('funding_bp', 1),
           'conditions': [['funding_bp', '<=', -2.0], ['buy_ratio_pct_30d', '<=', 0.03]]},
}

NOTEBOOK = """YOU ARE THE TRADER of a crypto futures bot (30 liquid USDT perpetuals, long and short).
Below is YOUR NOTEBOOK: patterns you found and verified on 2022-01..2024-06 hourly data, results after costs.
R = profit in units of the risk taken (stop distance). You decide which alerts to trade.

P1. CASCADE BOUNCE (long, hold 24h, stop = 1.0 x the coin's average daily range)
   Trigger: 4 or more of the 20 coins fell more than 4.3% within 4 hours while their open interest
   dropped more than 4.7% - leveraged longs liquidated across the market. Buy the dropped coins.
   Result: +0.157R per trade over 892 trades, 58% winners.
   Better: many coins dumping at once; 24h volume above 1.5x normal (+0.15..0.22R); BTC down >2% in 24h
           (+0.11..0.18R); coin down >25% in 7 days (+0.28R); funding above +1 bp (+0.23R).
   Worse:  a single coin dumping alone (-0.11R, usually its own bad news); BTC almost flat while coins dump
           (-0.12R); normal volume (~0R); the 2022 bear market after FTX (~0R, cascades kept going).

B3. DISTRIBUTION ON MARKET BOUNCE (short, hold 48h, stop = 1.3 x the coin's average daily range)
   Trigger: BTC is down over 30 days (falling market), BTC bounced 1%+ in 24h, but the coin is 2.5%+ weaker
   than BTC, while its open interest grew 4%+ and 24h volume is above 1.5x normal - someone is selling the coin
   into the market bounce. Sell it.
   Result: +0.255R per trade over 62 trades, 61% winners; positive in 6 of 7 half-years.
   Better: retail long share high (30-day rank above 0.5: +0.35R, the crowd is buying the bounce); funding not positive (+0.38R);
           BTC 30d decline mild, above -9% (+0.38R).
   Worse:  BTC 30d decline deeper than -9% (+0.13R); very high volume above 2.1x normal (+0.12R).

P4. SHORT SQUEEZE (long, hold 48h, stop = 1.5 x the coin's average daily range)
   Trigger: funding is deeply negative (-2 bp per 8h or lower: shorts pay longs every 8 hours) and the share of retail
   accounts in longs is at the lowest 3% of its last 30 days - the crowd is maximally short and pays to stay short.
   Buy the coin: the shorts give up or get squeezed.
   Result: +0.113R per trade over 386 trades, 50% winners (winners are bigger than losers).

RISK: at most 6 open positions. Take every alert that fits your notebook - a liquidation cascade usually gives
several good trades at once, and on history taking them all earned more than picking one or two. Skip an
alert only when your notebook says its conditions are the weak ones.
"""

QUESTION = """It is {time} UTC. New alerts this hour: {n}.
Already holding: {held}. Free position slots: {free}.

MARKET: BTC {btc4:+.1f}% in 4h, {btc24:+.1f}% in 24h, {btc7:+.1f}% in 7d, {btc30:+.1f}% in 30d. Coins in a liquidation cascade in the last 3h: {breadth}.

ALERTS - pattern, side, coin, then: change 4h %, change 24h %, change 7d %, strength vs BTC 24h %, open interest 4h % / 24h %,
24h volume vs normal, aggressive-buy share 24h, funding bp, retail long share rank vs its last 30 days (0 = lowest, 1 = highest):
{table}

Every alert already meets its pattern's trigger. The side of each alert is fixed by its pattern (long = buy the coin, short = sell it).
Which alerts do you trade now (at most {free})? Answer with JSON only, the reason in Russian, at most 25 words:
{{"trade": ["COIN", ...], "reason": "одно короткое предложение"}}"""

OPS = {'<': np.less, '<=': np.less_equal, '>': np.greater, '>=': np.greater_equal}

# Имена закономерностей для человека: в Telegram, в «почему» сделки и в обзоре — они, а не коды
# (28.09.2026 владелец: «читаю сообщение от ИИ и не понимаю, что за P»). Коды остаются в журналах.
TITLES = {'P1': 'Отскок после ликвидаций', 'P2': 'Накопление при слабом BTC', 'B3': 'Продажа на отскоке',
          'P4': 'Сжатие шортистов'}
_CODES = re.compile(r'(?<!\w)([PРBВ])([1234])(?!\w)')     # латиница и кириллица: модель пишет и «Р1»
# Ярлыки таблицы обзора, которые модель переносит в русский текст (обзор 20:00 28.09: «OI», «buy share»;
# пробный обзор 30.09 со статусом закономерностей: «Открытый interest», «funding не глубоко отрицательный»).
_TERMS = [(re.compile(r'retail long share', re.I), 'доля розничных лонгов'),
          (re.compile(r'retail rank', re.I), 'ранг розничных лонгов'),
          (re.compile(r'buy share', re.I), 'доля агрессивных покупок'),
          (re.compile(r'(?<!\w)OI(?!\w)'), 'ОИ'),
          (re.compile(r'(?<!\w)open interest(?!\w)', re.I), 'открытый интерес'),
          (re.compile(r'(?<!\w)interest(?!\w)', re.I), 'интерес'),
          (re.compile(r'(?<!\w)funding(?!\w)', re.I), 'фандинг'),
          (re.compile(r'implied volatility', re.I), 'ожидаемая волатильность')]


def title(key):
    """«Отскок после ликвидаций (лонг, 24 ч)»."""
    pat = PATTERNS[key]
    return f"{TITLES.get(key, key)} ({'лонг' if pat['side'] == 'long' else 'шорт'}, {pat['hold']} ч)"


def humanize(text):
    """
    Коды закономерностей в тексте модели -> имена: «P1» -> «Отскок после ликвидаций».

    Код, за которым модель сама написала имя («P1 «Отскок после ликвидаций»», обзор 16:00
    28.09), просто убирается — иначе имя шло бы дважды подряд.
    """
    text = str(text or '')
    for key, title_ru in TITLES.items():
        letter = {'P': '[PР]', 'B': '[BВ]'}[key[0]]
        text = re.sub(rf'(?<!\w){letter}{key[1]}(?!\w)\s*[—–:-]?\s*(?=«?{re.escape(title_ru)})', '', text)

    def name(m):
        key = {'P': 'P', 'Р': 'P', 'B': 'B', 'В': 'B'}[m.group(1)] + m.group(2)
        return f'«{TITLES[key]}»' if key in TITLES else m.group(0)
    text = _CODES.sub(name, text)
    for pattern, ru in _TERMS:
        text = pattern.sub(ru, text)
    return text

# «Лучше/хуже» тетради флагами — как в research/ai_notebook_flags.py (docs п. 71, совпадение держит тест).
# Только в журнал решений: модели счёт не показывается. Вживую проверяется, держатся ли флаги P1
# (на valid держались, t 2.5; у P2 пусты).
FLAGS = {
    'P1': {'better': [('cascade_count_3h', '>=', 6), ('vol_z', '>', 1.5), ('btc_ret_24h', '<', -2.0),
                      ('ret_7d', '<', -25.0), ('funding_bp', '>', 1.0)],
           'worse': [('abs_btc_ret_24h', '<', 1.0), ('vol_z', '<', 1.2)]},
    'P2': {'better': [('btc_ret_7d', '>', 3.4), ('btc_ret_24h', '<', -2.1), ('ret_7d', '>', 16.0), ('vol_z', '>', 2.8)],
           'worse': [('btc_ret_7d', '<', 0.0)]},
    'B3': {'better': [('buy_ratio_pct_30d', '>', 0.5), ('funding_bp', '<=', 0.0), ('btc_ret_30d', '>', -9.0)],
           'worse': [('btc_ret_30d', '<=', -9.0), ('vol_z', '>', 2.1)]},
}


def flags_of(f, key):
    """Сигнал -> {'score': «лучше» − «хуже», 'better': [...], 'worse': [...]}; нет флагов у закономерности — {}."""
    if key not in FLAGS:
        return {}
    got = {'better': [], 'worse': []}
    for side in got:
        for name, op, value in FLAGS[key][side]:
            x = abs(f['btc_ret_24h']) if name == 'abs_btc_ret_24h' else f.get(name)
            if x is not None and pd.notna(x) and OPS[op](float(x), value):
                got[side].append(f'{name} {op} {value}')
    return {'score': len(got['better']) - len(got['worse']), **got}


def _mask(feat, conditions):
    m = np.ones(len(feat), bool)
    for name, op, value in conditions:
        col = feat[name].to_numpy(float)
        m &= OPS[op](col, float(value)) & ~np.isnan(col)
    return m


def _state_path():
    return os.path.join(config.DATA_DIR, 'llm_notebook_state.json')


def _load_state():
    try:
        return json.load(open(_state_path(), encoding='utf-8'))
    except Exception:                                  # noqa: BLE001
        return {}


def _save_state(state):
    path = _state_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, path)


def market(frames):
    """{пара: (таблица, признаки)} с широтой разгрузки — как в историческом прогоне."""
    import flow_features
    btc = frames.get('BTCUSDT')
    data = {p: (df, flow_features.features(df, btc)) for p, df in frames.items() if df is not None and len(df)}
    cascade = PATTERNS['P1']['conditions'][:2]
    # Широта — только по базовым 20: на них она мерилась (порог ≥ 4 из 20).
    breadth = flow_features.cascade_count(
        {p: pd.Series(_mask(f, cascade), index=f.index) for p, (df, f) in data.items() if p in POOL})
    for p, (df, f) in data.items():
        f['cascade_count_3h'] = breadth.reindex(f.index).to_numpy()
    return data


def pairs_of(key):
    """Монеты, на которых торгуется закономерность: B3 — основные 20, P1 — все 42, остальные — 30."""
    if PATTERNS[key].get('universe') == 'core':
        return POOL
    return UNIVERSE if key in ON_WIDE else POOL + EXTRA


def _order(data, t, item):
    """Порядок сигналов: по закономерностям в порядке тетради, внутри — по её признаку силы."""
    pair, key = item
    feature, sign = PATTERNS[key]['rank']
    return list(PATTERNS).index(key), sign * float(data[pair][1].at[t, feature])


def alerts_at(data, t):
    """Новые сигналы в час t (первый час серии), в порядке _order (он же — выбор механики)."""
    items = []
    for key, pat in PATTERNS.items():
        if key in OFF:
            continue
        allowed = pairs_of(key)
        for p, (df, f) in data.items():
            if t not in f.index or p not in allowed:
                continue
            i = f.index.get_loc(t)
            m = _mask(f.iloc[max(0, i - 1):i + 1], pat['conditions'])
            if m[-1] and not (len(m) > 1 and m[0]):
                items.append((p, key))
    items.sort(key=lambda x: _order(data, t, x))
    seen, out = set(), []
    for p, key in items:
        if p not in seen:
            seen.add(p)
            out.append((p, key))
    return out


def brief(data, t, items, held, free):
    btc = data['BTCUSDT'][1].loc[t]
    rows = []
    for p, key in items:
        f = data[p][1].loc[t]
        rows.append(f"{key} {PATTERNS[key]['side']:5s} {p.replace('USDT', ''):9s} {f['ret_4h']:+6.1f} {f['ret_24h']:+6.1f} {f['ret_7d']:+6.1f} "
                    f"{f['rel_24h']:+6.1f} {f['oi_chg_4h']:+5.1f}/{f['oi_chg_24h']:+5.1f} {f['vol_z']:5.2f} "
                    f"{f['taker_24h']:5.3f} {f['funding_bp']:+5.2f} {f['buy_ratio_pct_30d']:.2f}")
    return QUESTION.format(time=(t + pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'), n=len(items),
                           held=', '.join(p.replace('USDT', '') for p in held) or 'nothing', free=free,
                           btc4=btc['btc_ret_4h'], btc24=btc['btc_ret_24h'], btc7=btc['btc_ret_7d'], btc30=btc['btc_ret_30d'],
                           breadth=int(btc['cascade_count_3h']), table='\n'.join(rows))


def ask_model(question, timeout=600):
    """Решение модели без мысли — как в историческом прогоне. -> текст ответа."""
    import llm_server
    raw = (f'<|im_start|>system\n{NOTEBOOK}<|im_end|>\n<|im_start|>user\n{question}<|im_end|>\n'
           f'<|im_start|>assistant\n<think>\n\n</think>\n\n')
    out = llm_server._completion({'prompt': raw, 'n_predict': 160, 'temperature': 0.2, 'top_k': 20, 'top_p': 0.9,
                                  'stop': ['<|im_end|>'], 'cache_prompt': True}, timeout)
    return out.get('content') or ''


EXPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'Промт_ИИ_тетрадь.txt')


def export_text():
    """Промт тетради целиком — для docs/Промт_ИИ_тетрадь.txt (правило: промт меняется только с экспортом)."""
    return (
        'Промт ИИ в режиме тетради (LLM_MODE=notebook) — экспорт из Live_Bot/llm_notebook.py.\n'
        'Промт меняется только вместе с этим файлом: совпадение держит tests/test_llm_notebook.py.\n'
        'Пересобрать: python Live_Bot/llm_notebook.py export\n\n'
        'Модель без мысли (<think></think> в подсказке). Решение о сделках: n_predict 160,\n'
        'temperature 0.2; обзор рынка: n_predict 600, temperature 0.3.\n\n'
        '=== СИСТЕМА: ТЕТРАДЬ (NOTEBOOK) ===\n'
        f'{NOTEBOOK}\n'
        '=== ВОПРОС О СДЕЛКАХ (QUESTION): раз в час, если есть новые сигналы и места ===\n'
        f'{QUESTION}\n\n'
        '=== ОБЗОР РЫНКА (REVIEW): раз в 4 часа ===\n'
        f'{REVIEW}\n')


def _decide_job(ask, question, items):
    """
    В потоке тетради: вопрос модели -> её ответ, как есть.

    Строка «модель: N вход» — для ops/restart_when_idle.sh: готовый ответ ждёт
    цикла, и выкатка не перезапускает бота, пока цикл его не забрал.
    """
    text = ask(question)
    picks, _ = picks_from(text, items)
    log(f'   {NAME}: модель: {len(picks or [])} вход из {len(items)} сигналов — ответ заберёт ближайший цикл')
    return text


def _price_now(pair, client=None):
    """Цена сейчас — закрытие идущей 5-минутной свечи на бирже бота; None — цены нет."""
    try:
        from exchange import fetch_ohlcv
        df = fetch_ohlcv('5m', limit=12, symbol=pair, client=client)
        price = float(df['close'].iloc[-1]) if df is not None and len(df) else 0.0
        return price if price > 0 else None
    except Exception:                                  # noqa: BLE001
        return None


def picks_from(text, items):
    """
    Ответ модели -> (пары, причина); (None, '') — списка сделок в ответе нет.

    Модель без мысли рассуждает прямо в «reason» и упирается в n_predict: на
    test v2 так обрезаны 8 ответов из 261 — без закрывающей скобки, но со
    списком целиком (он идёт первым). Такой список берётся, причина — как есть.
    Ключ списка — "trade" или "buy" (прежнее имя).
    """
    try:
        parsed = json.loads(text[text.index('{'):text.rindex('}') + 1])
        chosen = parsed.get('trade', parsed.get('buy'))
        reason = str(parsed.get('reason') or '')
    except Exception:                                  # noqa: BLE001
        found = re.search(r'"(?:trade|buy)"\s*:\s*\[([^\]]*)\]', text)
        if not found:
            return None, ''
        chosen = re.findall(r'"([^"]*)"', found.group(1))
        cut = re.search(r'"reason"\s*:\s*"(.*)', text, re.S)
        reason = cut.group(1).strip() + '…' if cut else ''
    names = {p.replace('USDT', ''): p for p, _ in items}
    return [names[str(x).upper().replace('USDT', '')] for x in (chosen or [])
            if str(x).upper().replace('USDT', '') in names], reason


def to_signal(pair, key, feat, price, reason):
    """Сигнал брокеру: вход по рынку в сторону закономерности, стоп — доля суточного размаха, выход по сроку."""
    import settings_store as settings
    pat = PATTERNS[key]
    sgn = 1.0 if pat['side'] == 'long' else -1.0
    dist = pat['stop'] * float(feat['atr_d']) / 100.0
    entry = price * (1 + sgn * 0.001)        # лимит за рынком — исполняется сразу, тейкером
    stop = price * (1 - sgn * dist)
    target = price * (1 + sgn * 20 * dist)   # цели нет: выход по сроку, цель лишь далеко
    why = f"{title(key)}: {humanize(reason)}"
    return {
        'trading_pair': pair,
        'strategy': NAME,
        'market_price': price,
        'setup': {'type': 'LONG' if sgn > 0 else 'SHORT', 'start_price': price, 'end_price': price, 'size': 0.0,
                  'start_time': None, 'end_time': None},
        'trigger': {'zone': TITLES.get(key, key), 'entry_type': 'LIMIT', 'trigger_price': entry},
        'params': {
            'entry': entry, 'stop_loss': stop, 'take_profit_1': target, 'take_profit_2': target,
            'tp_targets': [target], 'tp_fractions': [1.0],
            'be_level': None, 'breakeven_after_tp': False,
            'max_same_direction': SLOTS,
            'max_hold_hours': pat['hold'],
            'risk_pct': settings.risk_pct(NAME),
            'rr': 20.0, 'sl_distance': abs(entry - stop),
            'invalidation': stop,
        },
        'htf_trend': 'BULLISH' if sgn > 0 else 'BEARISH',
        'llm': {
            'mode': 'notebook', 'pattern': key, 'analysis': why, 'why': why,
            'stop_why': f"{pat['stop']} средних суточных размаха ({float(feat['atr_d']):.1f}%) — как в замере",
            'tp_why': f"выход по сроку: {pat['hold']} ч",
            'rr': None, 'votes': None, 'confluence': None, 'trigger_when': 'now',
            'features': {k: (None if pd.isna(feat[k]) else round(float(feat[k]), 4))
                         for k in ('ret_4h', 'ret_24h', 'ret_7d', 'rel_24h', 'oi_chg_4h', 'oi_chg_24h', 'vol_z',
                                   'taker_24h', 'funding_bp', 'buy_ratio_pct_30d', 'atr_d')},
        },
    }


def _append_jsonl(name, record):
    path = os.path.join(config.DATA_DIR, name)
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: журнал {name} не записан ({exc})')


def _log_decision(record):
    """Строка на каждый час с сигналами: сигналы, выбор модели, выбор механики — для замера вклада модели."""
    _append_jsonl('llm_notebook_log.jsonl', record)


def _tail_jsonl(name, n):
    """Последние n записей журнала (битая строка пропускается)."""
    path = os.path.join(config.DATA_DIR, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as fh:
        lines = fh.readlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def page_payload(decisions=10, reviews=3):
    """
    Для страницы «Разбор ИИ» панели: что тетрадь делает сейчас — статус закономерностей, последние
    обзоры рынка и решения по сигналам (свежие сверху), по-русски и с именами, а не кодами.
    """
    def pair(p):
        return str(p).replace('USDT', '')
    rows = []
    for r in reversed(_tail_jsonl('llm_notebook_log.jsonl', decisions)):
        alerts = r.get('alerts') or []
        rows.append({'hour': r.get('hour'),
                     'alerts': [{'pair': pair(a.get('pair')), 'title': title(a['pattern']) if a.get('pattern') in PATTERNS
                                 else a.get('pattern')} for a in alerts],
                     'picks': [pair(p) for p in r.get('picks') or []],
                     'mechanical': [pair(p) for p in r.get('mechanical') or []],
                     'reason': humanize(r.get('reason') or ''), 'delay_min': r.get('delay_min'),
                     'entries': {pair(k): v for k, v in (r.get('entries') or {}).items()}})
    views = [{'hour': r.get('hour'), 'text': humanize(r.get('text') or ''), 'view': r.get('view') or {}}
             for r in reversed(_tail_jsonl('llm_notebook_reviews.jsonl', reviews))]
    return {'status': status_now(), 'decisions': rows, 'reviews': views,
            'patterns': [title(k) for k in PATTERNS if k not in OFF]}


def _notify_decision(items, picks, reason):
    """Сигналы — строкой на закономерность, по имени: «Отскок после ликвидаций (лонг, 24 ч): AVAX, ADA»."""
    try:
        import telegram_notify
        coins = {}
        for p, k in items:
            coins.setdefault(k, []).append(p.replace('USDT', ''))
        telegram_notify.llm_notebook_decision(
            [f"{title(k)}: {', '.join(c)}" for k, c in coins.items()],
            [p.replace('USDT', '') for p in picks], humanize(reason))
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: сообщение о решении не отправлено ({exc})')


# ── Модель — сбоку от цикла ──────────────────────────────────────────────────
# Цикл бота один на все стратегии: bot.trading_cycle — одна задача планировщика
# раз в 5 минут, второй её экземпляр он не запускает. Ответ модели на сигналы —
# 1.5–4 минуты, а в час обзора вопрос встал бы в очередь за обзором (ещё 5–7
# минут). Цикл, который ждёт модель дольше пяти минут, — пропущенный такт у
# ВСЕХ стратегий: ни их заявок, ни стопов. Поэтому вопросы тетради идут в её
# собственный поток, ответ забирает ближайший цикл (_collect). Поток один:
# модель одна (llama-server -np 1), и вопрос о сделке встаёт в очередь раньше
# обзора. Поток — daemon: остановка бота не ждёт модель (пул потоков ждал бы
# её при выходе до конца вопроса, до 15 минут).
ANSWER_MAX_MIN = 30       # ответ позже — вход уже не тот, что мерили (вход на открытии часа)

# ── Здоровье: неработающая стратегия выглядела бы как «сигналов нет» ─────────
HEALTH_FAILS = 2                   # подряд — и владельцу сообщение
HEALTH_QUIET_S = 6 * 3600          # не чаще раза в 6 ч
HEALTH_WHAT = {'data': 'нет часовых данных бирж — часы без проверки сигналов',
               'decision': 'модель не дала решения по сигналам — сделки пропущены',
               'review': 'обзор рынка не получен — модель, вероятно, недоступна'}
_health = {'fails': {}, 'last': {}, 'alerted': 0.0}
_health_lock = threading.Lock()


def _health_event(kind, ok, detail='', key=None):
    """
    Сбой или успех: часовые данные (data), решение модели (decision), обзор (review).
    HEALTH_FAILS сбоев одного вида подряд — сообщение владельцу, не чаще HEALTH_QUIET_S.
    key — чтобы один и тот же час, повторённый следующим циклом, не считался дважды.
    -> True, если сообщение ушло.
    """
    with _health_lock:
        fails = _health['fails']
        if ok:
            fails.pop(kind, None)
            _health['last'].pop(kind, None)
            return False
        if key is not None and _health['last'].get(kind) == key:
            return False
        _health['last'][kind] = key
        fails[kind] = fails.get(kind, 0) + 1
        if fails[kind] < HEALTH_FAILS or time.time() - _health['alerted'] < HEALTH_QUIET_S:
            return False
        _health['alerted'] = time.time()
        count = fails[kind]
    text = f"{HEALTH_WHAT[kind]}: {count} раза подряд" + (f" ({detail})" if detail else '')
    log(f'⚠️ {NAME}: {text}')
    try:
        import telegram_notify
        telegram_notify.llm_notebook_health(text)
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: сообщение о сбое не отправлено ({exc})')
    return True


_queue = queue.Queue()
_worker = {'thread': None}
_asked = []               # вопросы в пути и готовые ответы, которые ждут цикла


def _serve():
    while True:
        future, fn, args = _queue.get()
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(fn(*args))
        except BaseException as exc:                   # noqa: BLE001 — ошибку увидит тот, кто заберёт ответ
            future.set_exception(exc)


def _submit(fn, *args):
    """Работа для модели — в очередь потока тетради. -> Future (цикл его не ждёт)."""
    future = Future()
    _queue.put((future, fn, args))
    thread = _worker['thread']
    if thread is None or not thread.is_alive():
        _worker['thread'] = threading.Thread(target=_serve, name='llm-notebook', daemon=True)
        _worker['thread'].start()
    return future


# ── Обзор рынка: модель анализирует рынок для человека ──────────────────────
REVIEW_EVERY_H = 4
# Сколько ждать обзор (07.10.2026). Модель сервера читает его вопрос (~5000 токенов: 42 монеты, статус, настроение)
# 7.5–8 мин и пишет ответ ещё 2–6 мин — 10–13 мин при прежнем пределе 15: под нагрузкой обзор терялся
# (5 из 30 с 05.10) и владельцу дважды ушло ложное «модель, вероятно, недоступна». Обзор стартует в начале
# часа, следующий вопрос о сделках — через час, так что 25 мин очередь не задерживают.
REVIEW_TIMEOUT_S = 25 * 60

REVIEW = """It is {time} UTC. MARKET DASHBOARD of your coins (hourly data, closed hour).
BTC: {btc24}% in 24h, {btc7}% in 7d, {btc30}% in 30d. Coins in a liquidation cascade in the last 3h: {breadth}.
{mood}
Your open positions now: {held} (they close by your notebook rules - stop or time).
How to read a row: price change over 24h, 7d, 30d; "vs BTC" = the coin's 24h change minus BTC's; "OI" = open
interest change over 24h; "buy share" = aggressive (taker) buying as a share of 24h volume, 0.50 = balance;
"funding" in bp per 8h, +1.0 = the normal rate; "retail rank" = share of retail accounts in longs vs its own last
30 days (0 = lowest, 1 = highest); "volume" = 24h volume vs normal.
{table}

PATTERN STATUS NOW (computed by code from this data - trust it, do not re-derive the conditions yourself):
{status}

Write a short market review for the owner IN RUSSIAN (at most 900 characters): 1) the market regime and what
drives it now, with MARKET MOOD (fear in options, US demand, spot vs futures); 2) where the crowd and the aggressive
flow are; 3) which patterns of your notebook can trigger soon
and on which coins - ONLY from PATTERN STATUS: if a pattern cannot trigger now, say so plainly and do not promise
trades by it (trades open only on pattern alerts, never from this review); 4) the main risk for the next 24 hours.
Plain text, no tables. The owner does not know the codes: do not write P1, B3, P4 - call the patterns only by
their Russian names: «Отскок после ликвидаций» (P1), «Продажа на отскоке» (B3), «Сжатие шортистов» (P4). No English
labels from the table either - say «доля розничных лонгов», «доля агрессивных покупок», «открытый интерес».
Then on the LAST line write JSON only:
{{"regime": "rising|falling|sideways", "btc_24h": "up|down|flat", "watch": [{{"coin": "XXX", "side": "long|short", "why": "few words"}}]}}"""

_review = {'slot': None, 'busy': False}


def _v(x, fmt):
    """Число для обзора; нет значения — прочерк, а не «nan»."""
    return '—' if x is None or pd.isna(x) else format(float(x), fmt)


# Статус закономерностей для обзора (30.09.2026). Обзоры 29–30.09 обещали шорты «Продажи на отскоке»
# при BTC за 30 дней в плюсе (её условие — минус) и «Накопление при слабом BTC» при растущем BTC:
# условия модель выводила из таблицы сама и ошибалась. Теперь их считает код — модель пересказывает.
_MARKET_WIDE = ('cascade_count_3h', 'btc_ret_4h', 'btc_ret_24h', 'btc_ret_7d', 'btc_ret_30d')
_LABEL = {'ret_4h': ('4h change', '+.1f', '%'), 'oi_chg_4h': ('OI 4h', '+.1f', '%'),
          'rel_24h': ('vs BTC', '+.1f', '%'), 'oi_chg_24h': ('OI 24h', '+.1f', '%'),
          'vol_z': ('volume', '.1f', 'x'), 'funding_bp': ('funding', '+.1f', 'bp'),
          'buy_ratio_pct_30d': ('retail rank', '.2f', ''), 'btc_ret_24h': ('BTC 24h', '+.1f', '%'),
          'btc_ret_30d': ('BTC 30d', '+.1f', '%'),
          'cascade_count_3h': ('coins in a liquidation cascade in the last 3h', '.0f', '')}
_RANKS = ('buy_ratio_pct_30d',)                         # 0..1: «близость» к порогу меряется в четвертях


def _holds(x, op, value):
    return x is not None and pd.notna(x) and bool(OPS[op](float(x), float(value)))


def _condition_text(name, op, value, x):
    """«funding -0.9bp (needs <= -2.0bp)» — модели."""
    label, fmt, unit = _LABEL.get(name, (name, '+.2f', ''))
    need = '0' if float(value) == 0 else format(float(value), fmt)
    return f'{label} {_v(x, fmt)}{unit if pd.notna(x) else ""} (needs {op} {need}{unit})'


def _gap(name, value, x):
    """Насколько признак не дотянул до порога — чтобы назвать монеты, ближайшие к закономерности."""
    if x is None or pd.isna(x):
        return 99.0
    return abs(float(x) - float(value)) / (0.25 if name in _RANKS else max(abs(float(value)), 1.0))


def _status_items(data, t):
    """
    Что с каждой торгуемой закономерностью в час t — одно из трёх:
    blocked — не выполнено рыночное условие [(признак, оп, порог, значение)]; ready — монеты, у которых
    выполнено всё; near — две ближайшие монеты и чего им не хватает [(монета, [(признак, оп, порог, значение)])].
    """
    market = data['BTCUSDT'][1].loc[t]
    items = []
    for key, pat in PATTERNS.items():
        if key in OFF:
            continue
        blocked = [(n, op, v, market.get(n)) for n, op, v in pat['conditions']
                   if n in _MARKET_WIDE and not _holds(market.get(n), op, v)]
        item = {'key': key, 'side': pat['side'], 'blocked': blocked, 'ready': [], 'near': []}
        if not blocked:
            own = [c for c in pat['conditions'] if c[0] not in _MARKET_WIDE]
            coins = []
            for p in pairs_of(key):
                if p not in data or t not in data[p][1].index:
                    continue
                f = data[p][1].loc[t]
                unmet = [(n, op, v, f.get(n)) for n, op, v in own if not _holds(f.get(n), op, v)]
                coins.append((len(unmet), sum(_gap(n, v, x) for n, _, v, x in unmet), p, unmet))
            # Ближе всех — по суммарному недобору до порогов, а не по числу невыполненных условий: 30.09 «ближе всех
            # COTI» стояла с одним условием, но рангом доли лонгов 0.95 при нужных 0.03 — дальше NEAR с двумя.
            coins.sort(key=lambda x: (x[1], x[0]))
            item['ready'] = [p.replace('USDT', '') for n, _, p, _ in coins if n == 0]
            item['near'] = [(p.replace('USDT', ''), unmet) for _, _, p, unmet in coins[:2]]
        items.append(item)
    return items


def pattern_status(data, t):
    """Строка на каждую торгуемую закономерность — модели в обзор: может ли сработать, на ком, кто ближе всех."""
    lines = []
    for it in _status_items(data, t):
        head = f"- {it['key']} «{TITLES.get(it['key'], it['key'])}» ({it['side']}):"
        if it['blocked']:
            why = '; '.join(_condition_text(*c) for c in it['blocked'])
            lines.append(f'{head} CANNOT trigger this hour - {why}.')
        elif it['ready']:
            lines.append(f"{head} all conditions hold now on {', '.join(it['ready'])}.")
        else:
            near = '; '.join(f'{coin}: ' + ', '.join(_condition_text(*c) for c in unmet) for coin, unmet in it['near'])
            lines.append(f'{head} no coin meets it now. Closest - {near}.')
    return '\n'.join(lines)


# Тот же статус человеку — по-русски (Telegram «Сетапы ИИ»): почему тетрадь молчит и кто ближе к сигналу.
_LABEL_RU = {'ret_4h': ('изменение за 4 ч', '+.1f', '%'), 'oi_chg_4h': ('ОИ за 4 ч', '+.1f', '%'),
             'rel_24h': ('сила против BTC за сутки', '+.1f', '%'), 'oi_chg_24h': ('ОИ за сутки', '+.1f', '%'),
             'vol_z': ('объём к обычному', '.1f', '×'), 'funding_bp': ('фандинг', '+.1f', ' bp'),
             'buy_ratio_pct_30d': ('доля розничных лонгов, ранг месяца', '.2f', ''),
             'btc_ret_24h': ('BTC за сутки', '+.1f', '%'), 'btc_ret_30d': ('BTC за 30 дней', '+.1f', '%'),
             'cascade_count_3h': ('монет в каскаде за 3 ч', '.0f', '')}
_OP_RU = {'<': 'ниже', '<=': 'не выше', '>': 'выше', '>=': 'не ниже'}


def _condition_ru(name, op, value, x):
    label, fmt, unit = _LABEL_RU.get(name, (name, '+.2f', ''))
    need = '0' if float(value) == 0 else format(float(value), fmt)
    return (f"{label} {_v(x, fmt)}{unit if pd.notna(x) else ''} "
            f"(нужно {_OP_RU[op]} {need}{unit})").replace('-', '−')


def status_ru(data, t):
    """[строка на закономерность] — для человека."""
    lines = []
    for it in _status_items(data, t):
        head = f"«{TITLES.get(it['key'], it['key'])}» ({'лонг' if it['side'] == 'long' else 'шорт'})"
        if it['blocked']:
            lines.append(f"{head} — сейчас сработать не может: {'; '.join(_condition_ru(*c) for c in it['blocked'])}")
        elif it['ready']:
            lines.append(f"{head} — условия выполнены: {', '.join(it['ready'])}")
        else:
            near = '; '.join(f'{coin} — ' + ', '.join(_condition_ru(*c) for c in unmet) for coin, unmet in it['near'])
            lines.append(f'{head} — ни у одной монеты; ближе всех {near}')
    return lines


def mood_line():
    """
    Фон обзора из общего слоя market_mood (30.09.2026): страх по опционам, спрос США, доля спота. Торговых
    правил на этих данных нет (п. 82) — это контекст анализа. Нет свежей строки — так и сказано.
    """
    try:
        import market_mood
        m = market_mood.facts()
    except Exception:                                  # noqa: BLE001
        m = {}
    if not m:
        return 'MARKET MOOD: not available this hour.'
    share = m.get('btc_spot_share_24h')
    return (f"MARKET MOOD: BTC implied volatility from options (DVOL) {_v(m.get('dvol'), '.1f')} "
            f"({_v(m.get('dvol_chg_24h'), '+.1f')}% in 24h; rank {_v(m.get('dvol_pct_30d'), '.2f')} within 30 days, "
            f"1 = most fear of the month); US buyers on Coinbase pay {_v(m.get('btc_cb_prem_bp'), '+.1f')} bp over "
            f"Binance for BTC (negative = US selling); spot share of BTC turnover "
            f"{'—' if share is None else f'{share * 100:.0f}%'}.")


def review_question(data, t, held=()):
    btc = data['BTCUSDT'][1].loc[t]
    rows = []
    for p in UNIVERSE:
        if p not in data or t not in data[p][1].index:
            continue
        f = data[p][1].loc[t]
        # Подписи у каждого числа: столбцы из голых чисел модель путала (12:00 28.09 приняла фандинг
        # +1.00 bp за долю агрессивных покупок, ранг 0..1 — за «экстремальные зоны»).
        rows.append(f"{p.replace('USDT', '')}: 24h {_v(f['ret_24h'], '+.1f')}% | 7d {_v(f['ret_7d'], '+.1f')}% | "
                    f"30d {_v(f['ret_30d'], '+.1f')}% | vs BTC {_v(f['rel_24h'], '+.1f')}% | "
                    f"OI {_v(f['oi_chg_24h'], '+.1f')}% | buy share {_v(f['taker_24h'], '.2f')} | "
                    f"funding {_v(f['funding_bp'], '+.1f')}bp | retail rank {_v(f['buy_ratio_pct_30d'], '.2f')} | "
                    f"volume {_v(f['vol_z'], '.1f')}x")
    # Открытые позиции — в вопрос: без них обзор 20:00 28.09 написал «открытых позиций нет» при LINK и XLM.
    return REVIEW.format(time=(t + pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'),
                         btc24=_v(btc['btc_ret_24h'], '+.1f'), btc7=_v(btc['btc_ret_7d'], '+.1f'),
                         btc30=_v(btc['btc_ret_30d'], '+.1f'), breadth=int(btc['cascade_count_3h']),
                         held=', '.join(p.replace('USDT', '') for p in held) or 'none',
                         table='\n'.join(rows), status=pattern_status(data, t), mood=mood_line())


def parse_review(text):
    """Обзор модели -> (текст для человека, её ожидания JSON или None)."""
    start = text.rfind('{"regime"')
    if start < 0:
        start = text.rfind('{\n"regime"')
    if start < 0 and '"regime"' in text:
        start = text.rfind('{', 0, text.rfind('"regime"'))
    if start < 0:
        return text, None
    try:
        end = text.rindex('}') + 1
        return text[:start].strip(), json.loads(text[start:end])
    except Exception:                                  # noqa: BLE001
        return text, None


def _run_review(question, slot, held):
    try:
        import llm_server
        raw = (f'<|im_start|>system\n{NOTEBOOK}<|im_end|>\n<|im_start|>user\n{question}<|im_end|>\n'
               f'<|im_start|>assistant\n<think>\n\n</think>\n\n')
        out = llm_server._completion({'prompt': raw, 'n_predict': 600, 'temperature': 0.3, 'top_k': 20,
                                      'top_p': 0.9, 'stop': ['<|im_end|>'], 'cache_prompt': True}, REVIEW_TIMEOUT_S)
        body, parsed = parse_review((out.get('content') or '').strip())
        _append_jsonl('llm_notebook_reviews.jsonl', {'hour': slot, 'held': held, 'text': body, 'view': parsed})
        _health_event('review', bool(body))
        body = humanize(body)                          # человеку — имена закономерностей, не коды
        log(f'   {NAME}: обзор рынка — {body[:160]}')
        try:
            import telegram_notify
            telegram_notify.llm_market_review(body)
        except Exception as exc:                      # noqa: BLE001
            log(f'   {NAME}: обзор не отправлен в Telegram ({exc})')
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: обзор рынка не получен ({exc})')
        _health_event('review', False, str(exc)[:120])
    finally:
        _review['busy'] = False


def _maybe_review(data, t, gate, run=None):
    """Раз в REVIEW_EVERY_H часов — обзор рынка в потоке тетради: цикл бота его не ждёт."""
    close = t + pd.Timedelta(hours=1)
    if close.hour % REVIEW_EVERY_H or _review['busy'] or _review['slot'] == close.isoformat():
        return False
    _review['slot'] = close.isoformat()
    _review['busy'] = True
    held = list(gate.held()) if hasattr(gate, 'held') else []
    try:
        question = review_question(data, t, held)
    except Exception as exc:                          # noqa: BLE001
        _review['busy'] = False
        log(f'   {NAME}: обзор рынка не собран ({exc})')
        # Несобранный вопрос — тот же «обзор не получен»: без этого обзоры молча прекратились бы.
        _health_event('review', False, f'вопрос не собран: {str(exc)[:100]}', key=close.isoformat())
        return False
    if run is not None:                                # тесты: без потока
        run(question, close.isoformat(), held)
        return True
    _submit(_run_review, question, close.isoformat(), held)
    return True


def scan(pairs, gate, client=None, balance=None, now_ms=None, frames_of=None, ask=None, price_of=None):
    """
    Каждый цикл: готовые ответы модели -> кандидаты брокеру (_collect).
    Раз в закрытый час: новые сигналы -> вопрос модели в поток тетради (_new_hour);
    ответ заберёт ближайший цикл — этот или следующий.

    frames_of(pair) -> часовая таблица (в бою flow_data.frame); ask(question) ->
    ответ модели (в бою ask_model); price_of(pair) -> цена сейчас (в бою
    _price_now, биржа бота). Тесты подменяют все три.
    """
    import flow_data
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    hour_ms = flow_data.last_closed_hour(now_ms)
    if _load_state().get('hour') != hour_ms:
        _new_hour(hour_ms, gate, now_ms, frames_of or (lambda p: flow_data.frame(p, now_ms)), ask or ask_model)
    return _collect(gate, now_ms, price_of or (lambda p: _price_now(p, client)))


def _new_hour(hour_ms, gate, now_ms, frames_of, ask):
    """Сигналы закрытого часа; есть сигналы и места — вопрос модели уходит в поток тетради."""
    # Данные — по всей вселенной тетради (42 пары; широта разгрузки — по базовым 20).
    # Четыре потока: первое чтение после перезапуска — 1000 часов по каждой паре.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = dict(zip(UNIVERSE, pool.map(frames_of, UNIVERSE)))
    if frames.get('BTCUSDT') is None:
        log(f'   {NAME}: нет часовых данных BTC — пропуск часа')
        _health_event('data', False, 'BTC', key=hour_ms)
        return
    _health_event('data', True)
    data = market(frames)
    t = pd.Timestamp(hour_ms, unit='ms', tz='UTC')
    # Торгует своя вселенная тетради (UNIVERSE), а не общий список ликвидных пар бота:
    # это настройка стратегии ИИ, на другие стратегии она не влияет.
    items = [(p, k) for p, k in alerts_at(data, t) if p in UNIVERSE and not gate.has_position_or_order(p)]
    state = {'hour': hour_ms, 'alerts': [f'{k} {p}' for p, k in items]}
    held = list(gate.held()) if hasattr(gate, 'held') else []
    free = SLOTS - len(held)
    if items:
        record = {'hour': t.isoformat(), 'held': held, 'free': free,
                  'alerts': [{'pair': p, 'pattern': k, 'price': float(data[p][0].loc[t, 'c']),
                              'atr_d': float(data[p][1].at[t, 'atr_d']), **flags_of(data[p][1].loc[t], k)}
                             for p, k in items],
                  # Что взяла бы механика (сигналы по порядку тетради): против неё меряется выбор модели.
                  'mechanical': [p for p, _ in items][:max(0, free)]}
        if free <= 0:
            log(f"   {NAME}: сигналы {', '.join(state['alerts'])}, но мест нет ({len(held)}/{SLOTS})")
            _log_decision(dict(record, picks=[], reason='мест нет'))
        else:
            question = brief(data, t, items, held, free)
            _asked.append({'hour_ms': hour_ms, 'items': items, 'record': record,
                           'feats': {p: data[p][1].loc[t] for p, _ in items},
                           'future': _submit(_decide_job, ask, question, items)})
            state['asked'] = True
            log(f"   {NAME}: сигналы {', '.join(state['alerts'])} — вопрос модели ушёл, "
                f"ответ заберёт ближайший цикл")
    try:                                              # человеку в Telegram «Сетапы ИИ»: почему тетрадь молчит
        state.update(status=status_ru(data, t), status_at=(t + pd.Timedelta(hours=1)).isoformat())
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: статус закономерностей не посчитан ({exc})')
    _save_state(state)
    # Обзор — после вопроса о сделке: в очереди к модели сделка первая.
    _maybe_review(data, t, gate)
    try:
        import news_feed                               # объявления биржи: пока только запись
        news_feed.poll()
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: новости не записаны ({exc})')


def status_now():
    """Статус закономерностей последнего разобранного часа — {'lines': [...], 'at': ISO закрытия} или {}."""
    state = _load_state()
    return {'lines': state['status'], 'at': state.get('status_at', '')} if state.get('status') else {}


def _note_state(hour_ms, **fields):
    """Ответ — в состояние своего часа; час уже сменился — не трогать."""
    state = _load_state()
    if state.get('hour') == hour_ms:
        state.update(fields)
        _save_state(state)


def _collect(gate, now_ms, price_of):
    """Готовые ответы модели -> кандидаты брокеру; ответ ещё в пути — его заберёт следующий цикл."""
    out = []
    for job in [j for j in _asked if j['future'].done()]:
        _asked.remove(job)
        out += _enter(job, gate, now_ms, price_of)
    return out


def _enter(job, gate, now_ms, price_of):
    """
    Ответ модели -> сигналы брокеру. Места и занятые пары — на момент входа, а
    не вопроса; цена — биржи сейчас, а не закрытия часа: брокер исполняет
    заявку за рынком по цене сигнала (strategy_profile.fills_through_market), и
    закрытие часа, которому 5–15 минут, дало бы вход, какого на бирже не было.
    """
    items = job['items']
    delay = round((now_ms - job['hour_ms'] - HOUR_MS) / 60000, 1)        # минут от закрытия часа
    record = dict(job['record'], delay_min=delay)
    try:
        text = job['future'].result()
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: модель не ответила ({getattr(exc, "llm_gate", "")} {exc}) — сигналы часа пропущены')
        _note_state(job['hour_ms'], error=str(exc))
        _log_decision(dict(record, picks=None, error=str(exc)))
        _health_event('decision', False, str(exc)[:120])
        return []
    picks, reason = picks_from(text, items)
    _note_state(job['hour_ms'], answer=text[:500], picks=picks)
    record.update(picks=picks, reason=reason, answer=text[:500])
    if picks is None:
        log(f'   {NAME}: ответ модели без JSON — сигналы часа пропущены: {text[:200]}')
        _log_decision(record)
        _health_event('decision', False, 'в ответе нет списка сделок')
        return []
    alerts = ', '.join(f'{k} {p}' for p, k in items)
    if delay > ANSWER_MAX_MIN:
        log(f'   {NAME}: сигналы {alerts}; ответ модели через {delay:.0f} мин после закрытия часа '
            f'(предел {ANSWER_MAX_MIN}) — вход пропущен')
        _log_decision(dict(record, late=True))
        _health_event('decision', False, f'ответ через {delay:.0f} мин после закрытия часа')
        return []
    _health_event('decision', True)
    held = list(gate.held()) if hasattr(gate, 'held') else []
    take = [p for p in picks if not gate.has_position_or_order(p)][:max(0, SLOTS - len(held))]
    keys = dict(items)
    out, entries = [], {}
    for p in take:
        price = price_of(p)
        if not price:
            log(f'   {NAME} {p}: нет цены биржи — вход пропущен')
            continue
        entries[p] = float(price)
        signal = to_signal(p, keys[p], job['feats'][p], float(price), reason)
        out.append({'pair': p, 'signal': signal, 'score': 1.0, 'rr': None, 'df_1h': None})
    _log_decision(dict(record, entries=entries))
    log(f"   {NAME}: сигналы {alerts}; модель берёт {', '.join(picks) or 'ничего'} — {reason}")
    _notify_decision(items, list(entries), reason)
    return out


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['export']:
        with open(EXPORT, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(export_text())
        print(EXPORT)
