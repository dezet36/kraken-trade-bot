"""
ИИ-трейдер по своей тетради (LLM_MODE=notebook, 27.09.2026).

Модель торгует сама — по закономерностям, которые нашла и проверила на
истории (docs/ИИ_замечания_на_проверку.md, п. 68–69):
    P1  отскок после широкой разгрузки — ≥ 4 из 20 монет за 3 ч упали > 4.3%
        за 4 ч при падении ОИ > 4.7%; лонг 24 ч, стоп 1.0 суточного размаха;
    P2  накопление при расхождении с BTC — монета сильнее BTC на 2.5%+ за
        сутки при падении BTC на 1%+, ОИ +4% за сутки, объём > 1.5 обычного;
        лонг 48 ч, стоп 1.3 суточного размаха (правило модели, круг 3).
Раз в час, на закрытии, код ищет НОВЫЕ сигналы (первый час серии) по парам
пула; есть — модель видит тетрадь, сводку по рынку и сигналам, что уже держит
и сколько мест свободно (всего 4), и решает, что купить. Вход — сразу по
рынку, выход — по стопу или по сроку закономерности.

Тетрадь, вопрос и исполнение — те же, что в историческом прогоне
research/ai_model_trader_bt.py (совпадение текста держит тест). Данные и
признаки — общий слой (flow_data, flow_features); правила решений — свои, здесь
(CLAUDE.md, «Изоляция стратегий»).
"""
import json
import os
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

import config
from logger import log

NAME = 'LLM'
SLOTS = 4


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

PATTERNS = {
    'P1': {'label': 'CASCADE BOUNCE', 'hold': 24, 'stop': 1.0,
           'conditions': [['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381], ['cascade_count_3h', '>=', 4]]},
    'P2': {'label': 'BTC DIVERGENCE ACCUMULATION', 'hold': 48, 'stop': 1.3,
           'conditions': [['rel_24h', '>=', 2.5], ['btc_ret_24h', '<=', -1.0], ['oi_chg_24h', '>=', 4.0],
                          ['vol_z', '>=', 1.5]]},
}

NOTEBOOK = """YOU ARE THE TRADER of a crypto futures bot (20 liquid USDT perpetuals, long only for now).
Below is YOUR NOTEBOOK: patterns you found and verified on 2022-01..2024-06 hourly data, results after costs.
R = profit in units of the risk taken (stop distance). You decide which alerts to trade.

P1. CASCADE BOUNCE (long, hold 24h, stop = 1.0 x the coin's average daily range)
   Trigger: 4 or more of the 20 coins fell more than 4.3% within 4 hours while their open interest
   dropped more than 4.7% - leveraged longs liquidated across the market. Buy the dropped coins.
   Result: +0.166R per trade over 637 trades, 59% winners.
   Better: many coins dumping at once; 24h volume above 1.5x normal (+0.15..0.22R); BTC down >2% in 24h
           (+0.11..0.18R); coin down >25% in 7 days (+0.28R); funding above +1 bp (+0.23R).
   Worse:  a single coin dumping alone (-0.11R, usually its own bad news); BTC almost flat while coins dump
           (-0.12R); normal volume (~0R); the 2022 bear market after FTX (~0R, cascades kept going).

P2. BTC DIVERGENCE ACCUMULATION (long, hold 48h, stop = 1.3 x the coin's average daily range)
   Trigger: the coin is 2.5%+ stronger than BTC over 24h while BTC fell 1%+, open interest grew 4%+ in 24h
   and 24h volume is above 1.5x normal - someone accumulates the coin while the market is weak.
   Result: +0.102R per trade over 486 trades, 48% winners (winners are bigger than losers).
   Better: BTC up more than 3.4% over 7 days (+0.25R); BTC down more than 2.1% today (+0.19R);
           coin up more than 16% over 7 days (+0.21R); 24h volume above 2.8x normal (+0.22R).
   Worse:  BTC down over 7 days (~0R) - in a falling market the strength usually fades.

RISK: at most 4 open positions. Positions opened together in one market move are one bet - prefer the
strongest alerts, and skip when your notebook says the conditions are the weak ones.
"""

QUESTION = """It is {time} UTC. New alerts this hour: {n}.
Already holding: {held}. Free position slots: {free}.

MARKET: BTC {btc4:+.1f}% in 4h, {btc24:+.1f}% in 24h, {btc7:+.1f}% in 7d. Coins in a liquidation cascade in the last 3h: {breadth}.

ALERTS - pattern, coin, then: change 4h %, change 24h %, change 7d %, strength vs BTC 24h %, open interest 4h % / 24h %,
24h volume vs normal, aggressive-buy share 24h, funding bp, retail long share percentile 30d:
{table}

Which alerts do you trade now (at most {free})? Answer with JSON only:
{{"buy": ["COIN", ...], "reason": "one sentence"}}"""

OPS = {'<': np.less, '<=': np.less_equal, '>': np.greater, '>=': np.greater_equal}


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
    breadth = flow_features.cascade_count(
        {p: pd.Series(_mask(f, cascade), index=f.index) for p, (df, f) in data.items()})
    for p, (df, f) in data.items():
        f['cascade_count_3h'] = breadth.reindex(f.index).to_numpy()
    return data


def alerts_at(data, t):
    """Новые сигналы в час t (первый час серии), по порядку: P1 по глубине падения, затем P2 по силе к BTC."""
    items = []
    for key, pat in PATTERNS.items():
        for p, (df, f) in data.items():
            if t not in f.index:
                continue
            i = f.index.get_loc(t)
            m = _mask(f.iloc[max(0, i - 1):i + 1], pat['conditions'])
            if m[-1] and not (len(m) > 1 and m[0]):
                items.append((p, key))
    f = {p: data[p][1] for p, _ in items}
    items.sort(key=lambda x: (x[1], f[x[0]].at[t, 'ret_4h'] if x[1] == 'P1' else -f[x[0]].at[t, 'rel_24h']))
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
        rows.append(f"{key} {p.replace('USDT', ''):9s} {f['ret_4h']:+6.1f} {f['ret_24h']:+6.1f} {f['ret_7d']:+6.1f} "
                    f"{f['rel_24h']:+6.1f} {f['oi_chg_4h']:+5.1f}/{f['oi_chg_24h']:+5.1f} {f['vol_z']:5.2f} "
                    f"{f['taker_24h']:5.3f} {f['funding_bp']:+5.2f} {f['buy_ratio_pct_30d']:.2f}")
    return QUESTION.format(time=(t + pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'), n=len(items),
                           held=', '.join(p.replace('USDT', '') for p in held) or 'nothing', free=free,
                           btc4=btc['btc_ret_4h'], btc24=btc['btc_ret_24h'], btc7=btc['btc_ret_7d'],
                           breadth=int(btc['cascade_count_3h']), table='\n'.join(rows))


def ask_model(question, timeout=600):
    """Решение модели без мысли — как в историческом прогоне. -> текст ответа."""
    import llm_server
    raw = (f'<|im_start|>system\n{NOTEBOOK}<|im_end|>\n<|im_start|>user\n{question}<|im_end|>\n'
           f'<|im_start|>assistant\n<think>\n\n</think>\n\n')
    out = llm_server._completion({'prompt': raw, 'n_predict': 160, 'temperature': 0.2, 'top_k': 20, 'top_p': 0.9,
                                  'stop': ['<|im_end|>'], 'cache_prompt': True}, timeout)
    return out.get('content') or ''


def picks_from(text, items):
    try:
        body = text[text.index('{'):text.rindex('}') + 1]
        parsed = json.loads(body)
    except Exception:                                  # noqa: BLE001
        return None, ''
    names = {p.replace('USDT', ''): p for p, _ in items}
    buy = [names[str(x).upper().replace('USDT', '')] for x in (parsed.get('buy') or [])
           if str(x).upper().replace('USDT', '') in names]
    return buy, str(parsed.get('reason') or '')


def to_signal(pair, key, feat, price, reason):
    """Сигнал брокеру: лонг по рынку, стоп — доля суточного размаха, выход по сроку."""
    import settings_store as settings
    pat = PATTERNS[key]
    dist = pat['stop'] * float(feat['atr_d']) / 100.0
    entry = price * 1.001                    # лимит за рынком — исполняется сразу, тейкером
    stop = price * (1 - dist)
    target = price * (1 + 20 * dist)         # цели нет: выход по сроку, цель лишь далеко
    why = f"{key} {pat['label'].lower()}: {reason}"
    return {
        'trading_pair': pair,
        'strategy': NAME,
        'market_price': price,
        'setup': {'type': 'LONG', 'start_price': price, 'end_price': price, 'size': 0.0,
                  'start_time': None, 'end_time': None},
        'trigger': {'zone': key, 'entry_type': 'LIMIT', 'trigger_price': entry},
        'params': {
            'entry': entry, 'stop_loss': stop, 'take_profit_1': target, 'take_profit_2': target,
            'tp_targets': [target], 'tp_fractions': [1.0],
            'be_level': None, 'breakeven_after_tp': False,
            'max_same_direction': SLOTS,
            'max_hold_hours': pat['hold'],
            'risk_pct': settings.risk_pct(NAME),
            'rr': 20.0, 'sl_distance': entry - stop,
            'invalidation': stop,
        },
        'htf_trend': 'BULLISH',
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


def scan(pairs, gate, client=None, balance=None, now_ms=None, frames_of=None, ask=None):
    """
    Раз в закрытый час: новые сигналы → решение модели → кандидаты брокеру.

    frames_of(pair) -> часовая таблица (в бою flow_data.frame); ask(question) ->
    ответ модели (в бою ask_model). Тесты подменяют оба.
    """
    import flow_data
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    hour_ms = flow_data.last_closed_hour(now_ms)
    state = _load_state()
    if state.get('hour') == hour_ms:
        return []
    frames_of = frames_of or (lambda p: flow_data.frame(p, now_ms))
    ask = ask or ask_model
    # Данные — по всем 20 парам тетради: широта разгрузки считалась по ним. Торгуем —
    # только пары из списка бота. Четыре потока: первое чтение после перезапуска —
    # 1000 часов по каждой паре, по очереди это минуты.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = dict(zip(POOL, pool.map(frames_of, POOL)))
    if frames.get('BTCUSDT') is None:
        log(f'   {NAME}: нет часовых данных BTC — пропуск часа')
        return []
    data = market(frames)
    t = pd.Timestamp(hour_ms, unit='ms', tz='UTC')
    items = [(p, k) for p, k in alerts_at(data, t) if p in pairs and not gate.has_position_or_order(p)]
    state = {'hour': hour_ms, 'alerts': [f'{k} {p}' for p, k in items]}
    held = list(gate.held()) if hasattr(gate, 'held') else []
    free = SLOTS - len(held)
    if not items:
        _save_state(state)
        return []
    if free <= 0:
        log(f"   {NAME}: сигналы {', '.join(state['alerts'])}, но мест нет ({len(held)}/{SLOTS})")
        _save_state(state)
        return []
    question = brief(data, t, items, held, free)
    try:
        text = ask(question)
    except Exception as exc:                          # noqa: BLE001
        log(f'   {NAME}: модель не ответила ({getattr(exc, "llm_gate", "")} {exc}) — сигналы часа пропущены')
        state['error'] = str(exc)
        _save_state(state)
        return []
    picks, reason = picks_from(text, items)
    state.update({'answer': text[:500], 'picks': picks})
    _save_state(state)
    if picks is None:
        log(f'   {NAME}: ответ модели без JSON — сигналы часа пропущены: {text[:200]}')
        return []
    log(f"   {NAME}: сигналы {', '.join(state['alerts'])}; модель берёт "
        f"{', '.join(picks) or 'ничего'} — {reason}")
    keys = dict(items)
    out = []
    for p in picks[:free]:
        f = data[p][1].loc[t]
        price = float(data[p][0].loc[t, 'c'])
        signal = to_signal(p, keys[p], f, price, reason)
        out.append({'pair': p, 'signal': signal, 'score': 1.0, 'rr': None, 'df_1h': None})
    return out
