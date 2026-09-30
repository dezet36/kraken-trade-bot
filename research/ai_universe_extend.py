"""
Тетрадь на 12 новых монетах (30.09.2026, docs/ИИ_замечания_на_проверку.md, п. 80) — условия записаны до расчёта.

Монеты выбраны по бирже, не по исходам: контракт USDT на Bybit и на Binance,
листинг на обеих до 30.06.2023, не среди 30 тетради, медиана дневного оборота
Bybit за 30 дней ≥ $3 млн. Переносятся P1 (широта — по основным 20) и P4,
B3 — нет. Проход закономерности на новой группе: R > 0 и в train, и в valid
при ≥ 30 сделках в каждом; 2021 — если там ≥ 20 сделок. Портфель v3.4
(механика, 6 мест): 30 пар против 30 + новые — сделок в неделю больше и сумма
R выше в train и valid; своё R сделок новых монет в valid ≥ +0.05R.

    FLOW_CACHE=flow_cache_new python research/ai_flow_fetch.py <пары>
    FLOW_START=2020-11-01 FLOW_END=2022-01-05 FLOW_CACHE=flow_cache_2021_new python research/ai_flow_fetch.py <пары>
    python research/ai_universe_extend.py coverage    # покрытие train ОИ и долей лонгов — до исходов
    python research/ai_universe_extend.py std         # train и valid: перенос P1 и P4, портфель
    python research/ai_universe_extend.py 2021        # 2021: перенос (справочно при < 20 сделок)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ['2021']:
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'      # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_model_trader_bt as B                        # noqa: E402
import ai_pattern_lab as L                            # noqa: E402
import ai_question_v34 as V4                          # noqa: E402

NEW = ('XMRUSDT', 'DASHUSDT', 'HBARUSDT', 'CRVUSDT', 'ICPUSDT', 'LDOUSDT', 'ALGOUSDT', 'STXUSDT', 'GALAUSDT',
       'ZENUSDT', 'EGLDUSDT', 'SANDUSDT')
TRANSFER = ('P1', 'P4')
MIN_TRADES, MIN_2021, MIN_OWN_R = 30, 20, 0.05
NOTEBOOK = {'P1': B.PATTERNS['P1'], 'B3': B.PATTERNS['B3'], 'P4': V4.P4}      # v3.4: P2 выключена


def out(name, lines):
    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', name), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


def kept_pairs():
    """Пары, у которых в train не меньше половины часов с ОИ и долей лонгов (правило п. 80 — до исходов)."""
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS['train'])
    hours = (b - a) / pd.Timedelta(hours=1)
    rows = []
    for p in NEW:
        df = pd.read_pickle(os.path.join(HERE, 'flow_cache_new', f'{p}.pkl'))
        part = df[(df.index >= a) & (df.index < b)]
        share = {c: part[c].notna().sum() / hours for c in ('c', 'oi', 'buy_ratio', 'funding')}
        rows.append((p, share, share['oi'] >= 0.5 and share['buy_ratio'] >= 0.5))
    return rows


def coverage():
    lines = ['Покрытие train новыми монетами (доля часов периода): свечи / ОИ / доля лонгов / фандинг — '
             'проход: ОИ и доля лонгов ≥ 50%']
    for p, s, ok in kept_pairs():
        lines.append(f"  {p:10s} {s['c']:4.0%} / {s['oi']:4.0%} / {s['buy_ratio']:4.0%} / {s['funding']:4.0%}  "
                     f"{'да' if ok else 'ВЫПАДАЕТ'}")
    out('ai_universe_extend_coverage.txt', lines)


def load(folder, btc, breadth, pairs):
    got = {}
    for p in pairs:
        path = os.path.join(HERE, folder, f'{p}.pkl')
        if not os.path.exists(path):
            continue
        df = pd.read_pickle(path)
        df = df[df['v'] > 0] if len(df) else df
        if not len(df):
            continue
        f = L.features(df, btc)
        f['cascade_count_3h'] = breadth.reindex(f.index).to_numpy()
        got[p] = (df, f)
    return got


def rule_of(key):
    pat = NOTEBOOK[key]
    return {'side': pat['side'], 'hold_hours': pat['hold'], 'stop_atr': pat['stop'], 'conditions': pat['conditions']}


def alerts_u(data, split, allowed):
    """B.alerts с разрешёнными монетами на каждую закономерность; порядок — как у B.alerts."""
    saved, by_hour = B.PATTERNS, {}
    try:
        for key in saved:
            B.PATTERNS = {key: saved[key]}
            for t, items in B.alerts({p: data[p] for p in allowed[key] if p in data}, split):
                by_hour.setdefault(t, []).extend(items)
    finally:
        B.PATTERNS = saved
    evs = []
    for t in sorted(by_hour):
        items = sorted(by_hour[t], key=lambda x: (list(saved).index(x[1]),
                                                  saved[x[1]]['rank'][1] * data[x[0]][1].at[t, saved[x[1]]['rank'][0]]))
        seen, uniq = set(), []
        for p, key in items:
            if p not in seen:
                seen.add(p)
                uniq.append((p, key))
        evs.append((t, uniq))
    return evs


def portfolio(data, split, allowed):
    evs = alerts_u(data, split, allowed)
    res = B.outcomes(data, evs)
    picks = []

    def take_all(t, cand, held, free):
        picks.extend((t, p) for p, _ in cand[:free])
        return [p for p, _ in cand]
    B.play(evs, res, take_all)
    return [(t, p, res[(t, p)][0], res[(t, p)][2]) for t, p in picks]


def std():
    B.PATTERNS = dict(NOTEBOOK)
    data = B.prepare()                                # 30 пар тетради, широта по основным 20
    base = list(data)
    core = [p for p in base if p not in B.EXTRA]
    btc = data['BTCUSDT'][0]
    breadth = data['BTCUSDT'][1]['cascade_count_3h']
    kept = [p for p, _, ok in kept_pairs() if ok]
    new = load('flow_cache_new', btc, breadth, kept)
    lines = [f"Тетрадь v3.4 на новых монетах (п. 80): {len(new)} из {len(NEW)} — {', '.join(x.replace('USDT', '') for x in new)}",
             f'Перенос закономерности: R > 0 в train и valid при ≥ {MIN_TRADES} сделках (все сигналы, без мест)']
    moved = []
    for key in TRANSFER:
        s_new, _ = L.evaluate(rule_of(key), data=new, splits=('train', 'valid'))
        s_old, _ = L.evaluate(rule_of(key), data=data, splits=('train', 'valid'))
        ok = all(s_new[x]['n'] >= MIN_TRADES and s_new[x]['r'] > 0 for x in ('train', 'valid'))
        moved += [key] if ok else []
        lines += [L.line(f"{key} на новых {len(new)}", s_new) + ('  ПЕРЕНОСИТСЯ' if ok else '  нет'),
                  L.line(f'{key} на 30 тетради (для сравнения)', s_old)]
    lines.append(f"Переносятся: {', '.join(moved) or 'ничего'}")

    both = {**data, **new}
    was = {key: (core if NOTEBOOK[key].get('universe') == 'core' else base) for key in NOTEBOOK}
    ext = {key: was[key] + (list(new) if key in moved else []) for key in NOTEBOOK}
    lines.append('\nПортфель v3.4, механика, 6 мест («брать все»): 30 пар против 30 + новые')
    verdict = bool(moved)
    for split in ('train', 'valid'):
        a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
        weeks = (b - a).days / 7
        if split == 'train':                          # самопроверка: разбиение по закономерностям = B.alerts
            assert alerts_u(data, split, was) == B.alerts(data, split)
        rows = {}
        for label, allowed in (('30 пар', was), ('30 + новые', ext)):
            got = portfolio(both, split, allowed)
            r = np.array([x[2] for x in got])
            eq = np.cumsum(r)
            own = np.array([x[2] for x in got if x[1] in new])
            rows[label] = (len(r) / weeks, r.sum(), own)
            lines.append(f"  {split:6s} {label:11s} {len(r):4d} сд {len(r) / weeks:4.1f}/нед сумма {r.sum():+6.1f}R "
                         f"на сделку {r.mean():+.3f} просадка {float(np.min(eq - np.maximum.accumulate(eq))):6.1f}R  "
                         f"({', '.join(f'{k} {sum(1 for x in got if x[3] == k)}' for k in NOTEBOOK)})"
                         + (f"; новых монет {len(own)} сд, на сделку {own.mean():+.3f}" if len(own) else ''))
        more = rows['30 + новые'][0] > rows['30 пар'][0] and rows['30 + новые'][1] > rows['30 пар'][1]
        own = rows['30 + новые'][2]
        own_ok = split != 'valid' or (len(own) > 0 and own.mean() >= MIN_OWN_R)
        verdict &= more and own_ok
        lines.append(f"  {split}: сделок больше и сумма выше — {'да' if more else 'НЕТ'}"
                     + (f"; своё R новых монет ≥ +{MIN_OWN_R} — {'да' if own_ok else 'НЕТ'}" if split == 'valid' else ''))
    lines.append(f"\nИтог (train и valid): {'ПРИНЯТО — ' + ', '.join(moved) + ' на новых монетах' if verdict else 'не принято'}")
    out('ai_universe_extend_std.txt', lines)


def y2021():
    import ai_notebook_2021 as Y                      # ставит L.SPLITS['y2021'] и готовит данные 2021
    B.PATTERNS = dict(NOTEBOOK)
    data = Y.prepare()
    btc = data['BTCUSDT'][0]
    breadth = data['BTCUSDT'][1]['cascade_count_3h']
    new = load('flow_cache_2021_new', btc, breadth, NEW)
    lines = [f"2021 (п. 80): новых монет с данными — {', '.join(x.replace('USDT', '') for x in new) or 'нет'} "
             f"(у Bybit они с осени 2021); проход при ≥ {MIN_2021} сделках — R > 0, иначе справочно"]
    for key in TRANSFER:
        s, _ = L.evaluate(rule_of(key), data=new, splits=('y2021',))
        y = s['y2021']
        mark = ('да' if y['r'] > 0 else 'НЕТ') if y['n'] >= MIN_2021 else 'справочно'
        lines.append(f"  {key}: {y['n']:3d} сд R {y['r'] if y['n'] else float('nan'):+.3f} "
                     f"t {y['t'] if y['n'] > 1 else float('nan'):+.1f} — {mark}")
    out('ai_universe_extend_2021.txt', lines)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    {'coverage': coverage, 'std': std, '2021': y2021}[sys.argv[1]]()
