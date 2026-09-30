"""
Круг 7: новые виды данных — спот против фьючерса, премия Coinbase, DVOL (30.09.2026, п. 82).
Условия и гипотезы записаны до расчёта (docs/ИИ_замечания_на_проверку.md, п. 82).

    python research/ai_round7.py hyp          # мои H1–H5: train и valid на 20 основных; прошедшие обе — на 2021
    python research/ai_round7.py model        # круг 7 модели (сервер): 5 правил, train и valid
    python research/ai_round7.py y2021 [имена]  # правила круга 7, прошедшие train и valid, — на 2021
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ['y2021'] or sys.argv[1:2] == ['hyp2021']:
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'      # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_new_sources as S                            # noqa: E402

L = S.install()
MIN_2021 = 20

# Гипотезы п. 82: (признак, оп, перцентиль train) — порог считается по 20 основным монетам на train.
HYP = {
    'H1_leverage_pump_short': ('short', 24, 1.0, [('ret_24h', '>=', 0.90), ('spot_share_rel', '<=', 0.25),
                                                  ('funding_bp', '>=', 0.75)]),
    'H2_us_demand_long': ('long', 48, 1.3, [('cb_prem_bp', '>=', 0.90), ('cb_prem_chg_24h', '>=', 0.75),
                                            ('ret_24h', '<=', 0.75)]),
    'H3_fear_spike_long': ('long', 24, 1.0, [('dvol_chg_24h', '>=', 0.95), ('ret_24h', '<=', 0.10)]),
    'H4_spot_buys_dip_long': ('long', 48, 1.3, [('ret_7d', '<=', 0.10), ('taker_gap_24h', '>=', 0.90),
                                                ('funding_bp', '<=', 0.50)]),
    'H5_us_selling_short': ('short', 48, 1.3, [('btc_cb_prem_bp', '<=', 0.10), ('rel_24h', '<=', 0.25),
                                               ('btc_ret_7d', '<=', None)]),
}


def train_quantiles(names):
    data = L.load_all()
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS['train'])
    allf = pd.concat([f[(f.index >= a) & (f.index < b)] for _p, (_d, f) in data.items()])
    return {n: allf[n].dropna() for n in names}


def rules_hyp():
    need = {c[0] for *_x, conds in HYP.values() for c in conds if c[2] is not None}
    q = train_quantiles(need)
    rules = []
    for name, (side, hold, stop, conds) in HYP.items():
        out = []
        for feat, op, p in conds:
            value = 0.0 if p is None else float(q[feat].quantile(p))
            out.append([feat, op, round(value, 6)])
        rules.append({'name': name, 'side': side, 'hold_hours': hold, 'stop_atr': stop, 'conditions': out})
    return rules


def passed(s):
    tr, va = s['train'], s['valid']
    return (tr['n'] and tr['r'] > 0 and (tr['t'] or 0) >= 2.0 and tr['per_week'] >= 0.5
            and va['n'] >= 20 and va['r'] > 0)


def hyp():
    rules = rules_hyp()
    lines = ['Круг 7, мои гипотезы (п. 82) на 20 основных монетах: проход train R > 0, t ≥ 2, ≥ 0.5/нед; '
             'valid R > 0 при ≥ 20']
    keep = []
    for r in rules:
        s, _ = L.evaluate(r, splits=('train', 'valid'))
        ok = passed(s)
        keep += [r] if ok else []
        lines.append(L.line(r['name'], s) + ('  ПРОШЛА train+valid' if ok else ''))
        lines.append(f"    условия: {json.dumps(r['conditions'], ensure_ascii=False)}")
    lines.append(f"Прошли train и valid: {', '.join(r['name'] for r in keep) or 'нет'}")
    text = '\n'.join(lines)
    print(text, flush=True)
    out = os.path.join(HERE, 'results', 'ai_round7_hyp.txt')
    open(out, 'w', encoding='utf-8').write(text + '\n')
    json.dump(rules, open(os.path.join(HERE, 'results', 'ai_round7_hyp_rules.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


def rules_2021(names, source):
    import ai_notebook_2021 as Y                      # ставит L.SPLITS['y2021'] и готовит данные 2021
    data = S.enrich(Y.prepare())
    lines = [f'2021 (п. 82), {source}: проход R > 0 при ≥ {MIN_2021} сделках']
    for r in names:
        s, _ = L.evaluate(r, data=data, splits=('y2021',))
        y = s['y2021']
        ok = y['n'] >= MIN_2021 and y['r'] > 0
        lines.append(f"  {'да ' if ok else 'НЕТ'} {r['name']:32s} {y['n']:4d} сд "
                     f"R {y['r'] if y['n'] else float('nan'):+.3f} t {y['t'] if y['n'] > 1 else float('nan'):+.1f}")
    text = '\n'.join(lines)
    print(text, flush=True)
    return text


def model_round():
    import ai_model_research as M
    S.install()
    M.FOCUS[7] = """
FOCUS OF THIS ROUND - NEW DATA. You now also see data you never had: the SPOT market next to the futures market
(spot_share_24h, spot_share_rel, spot_taker_24h, taker_gap_24h), the US price premium on Coinbase (cb_prem_bp,
cb_prem_chg_24h, btc_cb_prem_bp) and BTC implied volatility from options (dvol, dvol_chg_24h, dvol_pct_30d).
With futures-only features your 45 earlier rules found one lasting pattern. Every rule of this round must use at
least one of the NEW features and name the mechanism: who is buying or selling (spot holders, leveraged futures
traders, US buyers, option hedgers) and why price should follow. Most wanted: SHORT rules that make money when
BTC fell over 30 days (btc_ret_30d < 0), and LONG rules that are not liquidation cascades. Each rule must trade at
least 0.5 times per week across the 20 coins on 2022-01..2024-06.
"""
    sys.argv = [sys.argv[0], 'round', '7']
    M.main()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1]
    if cmd == 'hyp':
        hyp()
    elif cmd == 'hyp2021':
        rules = [r for r in json.load(open(os.path.join(HERE, 'results', 'ai_round7_hyp_rules.json'), encoding='utf-8'))
                 if r['name'] in sys.argv[2:]]
        text = rules_2021(rules, 'мои гипотезы')
        open(os.path.join(HERE, 'results', 'ai_round7_hyp_2021.txt'), 'w', encoding='utf-8').write(text + '\n')
    elif cmd == 'model':
        model_round()
    elif cmd == 'y2021':
        items = json.load(open(os.path.join(HERE, 'results', 'model_research', 'round7_scored.json'), encoding='utf-8'))
        chosen = [x['rule'] for x in items if (x['rule']['name'] in sys.argv[2:] if sys.argv[2:] else passed(x))]
        text = rules_2021(chosen, 'правила модели')
        open(os.path.join(HERE, 'results', 'ai_round7_model_2021.txt'), 'w', encoding='utf-8').write(text + '\n')
