"""SMC с нуля — сетапы и портфельный прогон.

Модель: снятие внешнего пула → (слом структуры) → вход → стоп за экстремумом
снятия → цель (R или противоположный пул).

Все варианты — словарь cfg поверх DEFAULT. Прогон:
    trades = run(cfg, 'dev')           # DataFrame сделок
    report(trades)                     # сводка
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smc0_data as D
import smc0_engine as E

DEFAULT = dict(
    # структура
    k_ext=5, ext_lookback=48, reclaim_bars=2, min_age=6, k_int=2,
    # вход: mkt | fvg | fvg_ce | ob | ote | half
    entry='mkt', mss_bars=12, fvg_pick='last',
    # стоп / цель
    stop_buf=0.1, target='rr', rr=2.0, min_rr=1.5, max_rr=6.0,
    expiry=12, max_hold=72,
    min_stop_pct=0.4, max_stop_pct=10.0,
    # фильтры (None — выкл.)
    depth_min=None, depth_max=None, oi_sweep_max=None, oi_sweep_min=None,
    crowd=None, regime=None, side=None, vol_min=None,
    # портфель
    cap=6,
    tf='1h',
    # модель: sweep (снятие → разворот) | bos (слом → откат → продолжение)
    model='sweep', k_bos=3, disp_min=1.5, oi_leg_min=None, oi_leg_max=None, discount=True,
    # режим бара для отчёта: окно коэффициента эффективности в барах
    er_bars=168,
)

_cache = {}


def _series(sym, period, folders, tf='1h'):
    key = (sym, period if isinstance(period, str) else tuple(period), tuple(folders or ()), tf)
    if key not in _cache:
        df = D.load(sym, period, folders)
        if df is not None and tf != '1h':
            df = D.resample(df, tf)
        _cache[key] = None if df is None else E.Series(df)
    return _cache[key]


_ev_cache = {}


def _events(S, key, cfg):
    k = (key, cfg['k_ext'], cfg['ext_lookback'], cfg['reclaim_bars'], cfg['min_age'], cfg['k_int'])
    if k not in _ev_cache:
        _ev_cache[k] = E.liquidity_events(S, k_ext=cfg['k_ext'], ext_lookback=cfg['ext_lookback'],
                                          reclaim_bars=cfg['reclaim_bars'], min_age=cfg['min_age'],
                                          k_int=cfg['k_int'])
    return _ev_cache[k]


def regime_at(S, t, bars=168):
    """Тренд/боковик по коэффициенту эффективности за 7 суток."""
    a = max(0, t - bars)
    net = S.c[t] - S.c[a]
    path = np.abs(np.diff(S.c[a:t + 1])).sum()
    er = abs(net) / path if path > 0 else 0
    if er >= 0.25:
        return 'up' if net > 0 else 'down'
    return 'range'


def _mss(S, ev, cfg):
    """Слом структуры после снятия. Возвращает бар слома u или None."""
    side, t, b0, ext = ev['side'], ev['t'], ev['b0'], ev['ext']
    ref = ev['ref']
    if ref is None:
        return None
    lvl = ref[0]
    for u in range(t, min(S.n, b0 + cfg['mss_bars'] + 1)):
        if side > 0:
            if S.l[u] < ext:
                return None
            if S.c[u] > lvl:
                return u
        else:
            if S.h[u] > ext:
                return None
            if S.c[u] < lvl:
                return u
    return None


def setups_for(S, sym, key, cfg):
    out = []
    evs = _events(S, key, cfg)
    for ev in evs:
        t = ev['t']
        if not S.sig_ok[t] or ev['b0'] < 30 or np.isnan(S.atr[t]):
            continue
        if S.gap[max(0, t - 24):t + 1].any():
            continue
        side = ev['side']
        if cfg['side'] is not None and side != cfg['side']:
            continue
        a = S.atr[t]
        depth = abs(ev['ext'] - ev['level']) / a
        if cfg['depth_min'] is not None and depth < cfg['depth_min']:
            continue
        if cfg['depth_max'] is not None and depth > cfg['depth_max']:
            continue
        b0 = ev['b0']
        oi_sw = S.oi[t] / S.oi[b0 - 1] - 1 if S.oi[b0 - 1] > 0 else 0.0
        if cfg['oi_sweep_max'] is not None and oi_sw > cfg['oi_sweep_max']:
            continue
        if cfg['oi_sweep_min'] is not None and oi_sw < cfg['oi_sweep_min']:
            continue
        vol_x = S.v[b0:t + 1].sum() / (S.vmed[t] * (t - b0 + 1)) if S.vmed[t] > 0 else 0
        if cfg['vol_min'] is not None and vol_x < cfg['vol_min']:
            continue
        fbp = S.fund_rate[t] * 1e4
        if cfg['crowd'] == 'against' and side * fbp > 0.5:
            continue
        if cfg['crowd'] == 'live' and side * fbp > -1.0:  # лонг ≤ −1 б.п., шорт ≥ +1 б.п.
            continue
        reg = regime_at(S, t, cfg['er_bars'])
        if cfg['regime'] is not None and not cfg['regime'](side, reg):
            continue
        # вход
        market = False
        t_sig = t
        stop = ev['ext'] - side * cfg['stop_buf'] * a
        mode = cfg['entry']
        if mode == 'mkt':
            entry = S.c[t]
            market = True
        elif mode == 'half':
            entry = (S.c[t] + ev['ext']) / 2
        else:
            u = _mss(S, ev, cfg)
            if u is None or not S.sig_ok[u]:
                continue
            t_sig = u
            if mode in ('fvg', 'fvg_ce'):
                fv = E.find_fvg(S, side, ev['ext_bar'] + 1, u)
                if not fv:
                    continue
                j, lo, hi = fv[-1] if cfg['fvg_pick'] == 'last' else fv[0]
                if mode == 'fvg':
                    entry = hi if side > 0 else lo
                else:
                    entry = (lo + hi) / 2
            elif mode == 'ob':
                j = ev['ext_bar']
                ob = None
                for q in range(j, max(0, j - 6), -1):
                    if (side > 0 and S.c[q] < S.o[q]) or (side < 0 and S.c[q] > S.o[q]):
                        ob = q
                        break
                if ob is None:
                    continue
                entry = S.h[ob] if side > 0 else S.l[ob]
            elif mode == 'ote':
                if side > 0:
                    top = S.h[ev['ext_bar']:u + 1].max()
                    entry = top - 0.705 * (top - ev['ext'])
                else:
                    bot = S.l[ev['ext_bar']:u + 1].min()
                    entry = bot + 0.705 * (ev['ext'] - bot)
            elif mode == 'mss_mkt':
                entry = S.c[u]
                market = True
            else:
                raise ValueError(mode)
            # лимит должен стоять с нужной стороны цены
            if not market and side * (S.c[u] - entry) <= 0:
                continue
        risk = abs(entry - stop)
        if side * (entry - stop) <= 0:
            continue
        stop_pct = risk / entry * 100
        if stop_pct < cfg['min_stop_pct'] or stop_pct > cfg['max_stop_pct']:
            continue
        # цель
        if cfg['target'] == 'rr':
            target = entry + side * cfg['rr'] * risk
        else:
            seen = S.h[t:t_sig + 1].max() if side > 0 else S.l[t:t_sig + 1].min()
            cands = [p for p in ev['opp'] if side * (p - entry) >= cfg['min_rr'] * risk
                     and side * (p - seen) > 0]
            if side < 0:
                cands = sorted(cands, reverse=True)
            if cands:
                target = cands[0]
                if side * (target - entry) > cfg['max_rr'] * risk:
                    target = entry + side * cfg['max_rr'] * risk
            else:
                target = entry + side * cfg['rr'] * risk
        out.append({'sym': sym, 't': t_sig, 'ts': S.idx[t_sig], 'side': side, 'entry': entry,
                    'stop': stop, 'target': target, 'market': market, 'regime': reg,
                    'depth': depth, 'oi_sw': oi_sw, 'fund_bp': fbp, 'vol_x': vol_x,
                    'stop_pct': stop_pct, 'rr_plan': abs(target - entry) / risk})
    return out


def bos_setups(S, sym, cfg):
    """Продолжение: слом структуры (закрытие за последним подтверждённым
    свингом) с ногой-смещением → лимит от имбаланса/ордер-блока ноги →
    стоп за началом ноги → цель R или пул ликвидности за ней."""
    k = cfg['k_bos']
    ph, pl = E.pivots(S.h, S.l, k)
    sh = None  # (уровень, бар) последний подтверждённый свинг-хай, ещё не сломанный
    sl = None
    hi_pools, lo_pools = [], []  # все подтверждённые свинги как цели
    out = []
    h, l, c, o = S.h, S.l, S.c, S.o
    for t in range(S.n):
        s = t - k
        if s >= 0:
            if ph[s]:
                sh = (h[s], s)
                hi_pools.append(h[s])
            if pl[s]:
                sl = (l[s], s)
                lo_pools.append(l[s])
        hi_pools = [p for p in hi_pools if p >= h[t]]
        lo_pools = [p for p in lo_pools if p <= l[t]]
        for side in (1, -1):
            ref = sh if side > 0 else sl
            if ref is None:
                continue
            if not ((side > 0 and c[t] > ref[0]) or (side < 0 and c[t] < ref[0])):
                continue
            # свинг сломан — больше не используется
            if side > 0:
                sh = None
            else:
                sl = None
            if not S.sig_ok[t] or t < 30 or np.isnan(S.atr[t]) or S.gap[max(0, t - 24):t + 1].any():
                continue
            if cfg['side'] is not None and side != cfg['side']:
                continue
            a = S.atr[t]
            seg = slice(ref[1], t + 1)
            if side > 0:
                origin = ref[1] + int(np.argmin(l[seg]))
                ext = l[origin]
            else:
                origin = ref[1] + int(np.argmax(h[seg]))
                ext = h[origin]
            disp = side * (c[t] - ext) / a
            if disp < cfg['disp_min']:
                continue
            oi_leg = S.oi[t] / S.oi[origin] - 1 if S.oi[origin] > 0 else 0.0
            if cfg['oi_leg_min'] is not None and oi_leg < cfg['oi_leg_min']:
                continue
            if cfg['oi_leg_max'] is not None and oi_leg > cfg['oi_leg_max']:
                continue
            fbp = S.fund_rate[t] * 1e4
            if cfg['crowd'] == 'against' and side * fbp > 0.5:
                continue
            if cfg['crowd'] == 'live' and side * fbp > -1.0:
                continue
            reg = regime_at(S, t, cfg['er_bars'])
            if cfg['regime'] is not None and not cfg['regime'](side, reg):
                continue
            mode = cfg['entry']
            if mode in ('fvg', 'fvg_ce'):
                fv = E.find_fvg(S, side, origin + 1, t)
                if not fv:
                    continue
                j, lo_, hi_ = fv[0] if cfg['fvg_pick'] == 'first' else fv[-1]
                entry = (hi_ if side > 0 else lo_) if mode == 'fvg' else (lo_ + hi_) / 2
            elif mode == 'ob':
                ob = None
                for q in range(origin, max(0, origin - 6), -1):
                    if (side > 0 and c[q] < o[q]) or (side < 0 and c[q] > o[q]):
                        ob = q
                        break
                if ob is None:
                    continue
                entry = h[ob] if side > 0 else l[ob]
            elif mode == 'eq':  # равновесие ноги (50%)
                entry = (c[t] + ext) / 2
            elif mode == 'mkt':
                entry = c[t]
            else:
                raise ValueError(mode)
            # вход только в «дисконте» (для лонга — нижняя половина ноги)
            if cfg['discount'] and mode != 'mkt':
                mid = (c[t] + ext) / 2
                if side * (entry - mid) > 0:
                    entry = mid
            stop = ext - side * cfg['stop_buf'] * a
            risk = abs(entry - stop)
            if side * (entry - stop) <= 0 or (mode != 'mkt' and side * (c[t] - entry) <= 0):
                continue
            stop_pct = risk / entry * 100
            if stop_pct < cfg['min_stop_pct'] or stop_pct > cfg['max_stop_pct']:
                continue
            if cfg['target'] == 'rr':
                target = entry + side * cfg['rr'] * risk
            else:
                pools = hi_pools if side > 0 else lo_pools
                cands = sorted(p for p in pools if side * (p - entry) >= cfg['min_rr'] * risk
                               and side * (p - c[t]) > 0)
                if side < 0:
                    cands = cands[::-1]
                target = cands[0] if cands else entry + side * cfg['rr'] * risk
                if side * (target - entry) > cfg['max_rr'] * risk:
                    target = entry + side * cfg['max_rr'] * risk
            out.append({'sym': sym, 't': t, 'ts': S.idx[t], 'side': side, 'entry': entry,
                        'stop': stop, 'target': target, 'market': mode == 'mkt', 'regime': reg,
                        'depth': disp, 'oi_sw': oi_leg, 'fund_bp': fbp, 'vol_x': np.nan,
                        'stop_pct': stop_pct, 'rr_plan': abs(target - entry) / risk})
    return out


def run(cfg=None, period='dev', syms=None, folders=None):
    c = dict(DEFAULT)
    if cfg:
        c.update(cfg)
    syms = syms or D.POOL
    rows = []
    for sym in syms:
        S = _series(sym, period, folders, c['tf'])
        if S is None:
            continue
        key = (sym, period if isinstance(period, str) else tuple(period), c['tf'])
        sets = setups_for(S, sym, key, c) if c['model'] == 'sweep' else bos_setups(S, sym, c)
        busy_until = -1
        for s in sorted(sets, key=lambda x: x['t']):
            if s['t'] <= busy_until:
                continue
            r = E.simulate(S, s['side'], s['t'], s['entry'], s['stop'], s['target'],
                           c['expiry'], c['max_hold'], market=s['market'])
            if r is None:
                continue
            s = dict(s)
            s.update(r)
            if r['filled']:
                busy_until = r['exit']
                s['ts_exit'] = S.idx[r['exit']]
            else:
                busy_until = s['t'] + c['expiry']
                s['ts_exit'] = S.idx[min(S.n - 1, busy_until)]
            rows.append(s)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return apply_cap(df, c['cap'])


def apply_cap(df, cap):
    """Предел одновременных позиций/заявок по пулу (заявка занимает место с
    постановки до выхода или снятия)."""
    df = df.sort_values('ts').reset_index(drop=True)
    if not cap:
        return df
    keep = []
    open_ = []
    busy = {}
    for i, r in df.iterrows():
        open_ = [e for e in open_ if e > r['ts']]
        if len(open_) >= cap or busy.get(r['sym'], r['ts']) > r['ts']:
            keep.append(False)
            continue
        keep.append(True)
        open_.append(r['ts_exit'])
        busy[r['sym']] = r['ts_exit']
    return df[keep].reset_index(drop=True)


def boot_ci(x, weeks, n=2000, seed=1):
    """Доверительный интервал среднего с бутстрепом по неделям (сделки одной
    недели коррелируют)."""
    rng = np.random.default_rng(seed)
    g = pd.Series(x).groupby(weeks.values)
    sums = g.sum().values
    cnts = g.count().values
    k = len(sums)
    if k < 5:
        return (np.nan, np.nan)
    idx = rng.integers(0, k, (n, k))
    m = sums[idx].sum(1) / cnts[idx].sum(1)
    return tuple(np.percentile(m, [2.5, 97.5]))


def report(df, label='', span_weeks=None):
    f = df[df['filled']] if 'filled' in df else df
    if f.empty:
        return {'label': label, 'n': 0}
    wk = f['ts'].dt.to_period('W').astype(str)
    if span_weeks is None:
        span_weeks = max(1, (df['ts'].max() - df['ts'].min()).days / 7)
    lo, hi = boot_ci(f['R'].values, wk)
    eq = f['R'].cumsum()
    dd = (eq.cummax() - eq).max()
    out = {
        'label': label, 'orders': len(df), 'n': len(f), 'per_wk': len(f) / span_weeks,
        'fill%': 100 * len(f) / len(df), 'win%': 100 * (f['R'] > 0).mean(),
        'R/tr': f['R'].mean(), 'ci_lo': lo, 'ci_hi': hi, 'sumR': f['R'].sum(),
        'grossR/tr': f['gross_R'].mean(), 'costR/tr': f['cost_R'].mean(),
        'maxDD_R': dd, 'stop%': f['stop_pct'].median(),
        'long': (f['side'] > 0).sum(), 'short': (f['side'] < 0).sum(),
    }
    return out


def by(df, col):
    f = df[df['filled']]
    return f.groupby(col).agg(n=('R', 'size'), R=('R', 'mean'), sumR=('R', 'sum'),
                              win=('R', lambda x: (x > 0).mean()))
