"""
pair_qualify.py — квалификация всех живых пар единым формальным критерием.

Прогон: D1B + сессионный фильтр (деплойный конфиг — квалифицируем под то, что
реально торгуем), per-pair, БЕЗ portfolio_filter (мера индивидуального edge,
flat R), вся доступная история пары; eval_start = max(T0, листинг + 37д прогрева HTF).

Критерии (рамка утверждена пользователем):
- Критерий 0: символа нет на Bybit linear -> REMOVE-dead (бэктест не нужен).
- Исключение существующей (mo>=12): sumR<0 И PF_R<0.9 И h1<0 И h2<0.
- Fast-fail молодых (4<=mo<12):     sumR<=-15 И PF_R<=0.7 И h1<0 И h2<0.
- Включение нового кандидата:       mo>=12 И n>=40 И sumR>0 И PF_R>=1.0
                                    И min(h1,h2)>-10 И max(h1,h2)>0.
- Молодые существующие, не провалившие fast-fail -> PROBATION (остаются).

Запуск: python pair_qualify.py   (после bt12.py; 10 старых пар берутся из кэша
симуляций walk_forward, остальные досимулируются ~45-50 мин; повторно — мгновенно)
"""
import json
import os
import time

import pandas as pd

import backtest_campaign as camp
import bt12

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}
SESS = frozenset({12, 13, 14, 15, 16})
CONFIG_KEY = 'D1B_sess'
DEFAULTS = {'BLOCK_BIRTH_HOURS': frozenset(), 'BLOCK_FILL_HOURS': frozenset(),
            'ENTRY_R_E1A': None, 'BE_EXT': 0.0, 'HTF_STRICT': False,
            'DIR_CAP': None, 'TP1_EXT': 0.18, 'MIN_IMPULSE_PCT': 3.0, 'COOLDOWN_H': 12}

RISK, CAP = 0.5, 3   # для финального портфельного прогона нового пула


def flat(trades):
    if not trades:
        return dict(n=0, sumR=0.0, pf_r=None, wr=0.0)
    rs = [t['R'] for t in trades]
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return dict(n=len(rs), sumR=round(sum(rs), 1),
                pf_r=round(gp / gl, 3) if gl > 0 else None,
                wr=round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1))


FULL_WINDOW_MO = 11.5   # 360-дневное окно = 11.83 мес; порог «полного окна» чуть ниже


def classify(pair, is_existing, mo, m):
    n, sumr, pf, h1, h2 = m['n'], m['sumR'], (m['pf_r'] or 0.0), m['h1'], m['h2']
    if mo >= FULL_WINDOW_MO:
        if is_existing:
            if sumr < 0 and pf < 0.9 and h1 < 0 and h2 < 0:
                return 'REMOVE-perf'
            return 'KEEP'
        if n >= 40 and sumr > 0 and pf >= 1.0 and min(h1, h2) > -10 and max(h1, h2) > 0:
            return 'ADD'
        return 'NO-ADD'
    if mo >= 4:
        if is_existing:
            if sumr <= -15 and pf <= 0.7 and h1 < 0 and h2 < 0:
                return 'REMOVE-perf'
            return 'PROBATION'
        return 'PROBATION-cand'
    return 'PROBATION' if is_existing else 'NO-ADD'


def run():
    t0 = time.time()
    with open(os.path.join(bt12.CACHE_12M, 'manifest.json'), encoding='utf-8') as f:
        manifest = json.load(f)['pairs']

    t_end = max(pd.Timestamp(v['5m']['last']) for v in manifest.values() if v.get('5m', {}).get('n'))
    T0 = t_end - pd.Timedelta(days=bt12.EVAL_DAYS)

    for k, v in DEFAULTS.items():
        setattr(camp, k, v)
    camp.BLOCK_BIRTH_HOURS = SESS

    existing = set(bt12.CORE_PAIRS + bt12.YOUNG_PAIRS)
    rows, trades_by_pair = [], {}

    for pair in bt12.ALL_PAIRS:
        info = manifest.get(pair)
        if not info or not info.get('5m', {}).get('n'):
            print(f'{pair}: нет данных в манифесте — пропуск')
            continue

        cache_f = os.path.join(bt12.CACHE_12M, 'trades', f'{CONFIG_KEY}__{pair}.pkl')
        if os.path.exists(cache_f):
            trd = bt12.sim_trades(pair, None, None, None, CFG, CONFIG_KEY)
        else:
            df1, df5, df4 = bt12.load_pair(pair)
            trd = bt12.sim_trades(pair, df1, df5, df4, CFG, CONFIG_KEY)
            del df1, df5, df4

        first = pd.Timestamp(info['5m']['first'])
        last = pd.Timestamp(info['5m']['last'])
        eval_start = max(T0, first + pd.Timedelta(days=37))
        mo = (last - eval_start).days / 30.44

        tr = [t for t in trd if pd.Timestamp(t['entry_time']) >= eval_start]
        trades_by_pair[pair] = tr
        mid = eval_start + (last - eval_start) / 2
        m = flat(tr)
        m['h1'] = flat([t for t in tr if pd.Timestamp(t['entry_time']) < mid])['sumR']
        m['h2'] = flat([t for t in tr if pd.Timestamp(t['entry_time']) >= mid])['sumR']

        verdict = classify(pair, pair in existing, mo, m)
        rows.append({'pair': pair, 'existing': pair in existing, 'months': round(mo, 1),
                     'n': m['n'], 'n_per_mo': round(m['n'] / mo, 1) if mo > 0 else 0,
                     'sumR': m['sumR'], 'pf_r': m['pf_r'], 'wr': m['wr'],
                     'h1_sumR': m['h1'], 'h2_sumR': m['h2'], 'verdict': verdict})
        print(f"{pair:<15} mo={mo:5.1f} n={m['n']:<4} sumR={m['sumR']:+8.1f} "
              f"PF={m['pf_r'] or 0:5.2f} WR={m['wr']:4.1f}% "
              f"h1={m['h1']:+7.1f} h2={m['h2']:+7.1f} -> {verdict}")

    df = pd.DataFrame(rows).sort_values('sumR', ascending=False)
    df.to_csv(r'D:\Bot trade\backtest_pair_qual.csv', index=False)

    # ── Итоговый пул ──────────────────────────────────────────────────────────
    keep = [r['pair'] for r in rows if r['verdict'] in ('KEEP', 'PROBATION')]
    add  = [r['pair'] for r in rows if r['verdict'] == 'ADD']
    rem_perf = [r['pair'] for r in rows if r['verdict'] == 'REMOVE-perf']
    new_pool = keep + add

    print('\n===== ИТОГ КВАЛИФИКАЦИИ =====')
    print(f'REMOVE-dead (нет на бирже): {bt12.DEAD_SYMBOLS}')
    print(f'REMOVE-perf: {rem_perf or "нет"}')
    print(f'PROBATION:   {[r["pair"] for r in rows if r["verdict"].startswith("PROBATION")] or "нет"}')
    print(f'ADD:         {add or "нет"}')
    print(f'\nНовый TRADING_PAIRS_POOL ({len(new_pool)} пар):')
    for i in range(0, len(new_pool), 5):
        print('    ' + ', '.join(f"'{p}'" for p in new_pool[i:i+5]) + ',')

    # ── Портфельный прогон нового пула (cap=3, risk=0.5) + dir_cap ───────────
    pool_trades = [t for p in new_pool for t in trades_by_pair.get(p, [])]
    print(f'\n===== ПОРТФЕЛЬ нового пула ({len(new_pool)} пар, 12 мес, D1B+сессия) =====')
    for dc in (None, 2):
        capped, _, dropped = camp.portfolio_filter(pool_trades, cap=CAP, dir_cap=dc)
        capped, curve, ruined = camp.build_equity(capped, risk_pct=RISK)
        fm = flat(capped)
        pnl = (curve[-1] - curve[0]) / curve[0] * 100 if capped else 0
        peak = curve[0]; mdd = 0.0
        for b in curve:
            peak = max(peak, b); mdd = max(mdd, (peak - b) / peak * 100)
        print(f"  dir_cap={dc}: n={fm['n']} WR={fm['wr']}% sumR={fm['sumR']:+.1f} "
              f"PnL={pnl:+.1f}% DD={mdd:.1f}%{' RUIN' if ruined else ''} (dropped {dropped})")

    with open(r'D:\Bot trade\exp_pair_qual_pool.json', 'w', encoding='utf-8') as f:
        json.dump({'new_pool': new_pool, 'add': add, 'remove_perf': rem_perf,
                   'remove_dead': bt12.DEAD_SYMBOLS}, f, ensure_ascii=False, indent=2)
    print(f'\nСохранено: backtest_pair_qual.csv, exp_pair_qual_pool.json | '
          f'Время: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    run()
