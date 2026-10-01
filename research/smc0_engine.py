"""SMC с нуля (30.09.2026) — структура и исполнение.

Всё причинное: на баре t используется только то, что известно к закрытию t.

Структура (слой «находим и обозначаем»):
  * свинги — фрактал силы k, подтверждается через k баров;
  * пулы ликвидности — подтверждённые внешние свинги (экстремум за
    `ext_lookback` баров до себя), живут, пока цена их не пробила;
  * снятие — бар(ы) за пулом и закрытие обратно не позже `reclaim_bars`;
  * слом (MSS) — закрытие за последним внутренним свингом (k=2) против
    хода, который снял пул;
  * имбаланс (FVG) — разрыв трёх свечей в ноге слома; ордер-блок — последняя
    свеча против хода перед ногой слома.

Исполнение (консервативно):
  * лимитный вход наливается, только если цена прошла ЗА уровень на `thru`;
  * в баре налива стоп проверяется, цель — нет;
  * стоп и цель в одном баре — стоп;
  * заявка снимается, если цена дошла до цели раньше налива, или по сроку;
  * мейкер 0.02% на входе и цели, тейкер 0.055% + 0.03% проскальзывания на
    стопе, тейкер + 0.01% на выходе по времени, фандинг в 00/08/16 UTC.
"""
import numpy as np

MAKER = 0.0002
TAKER = 0.00055
SLIP_STOP = 0.0003
SLIP_TIME = 0.0001
THRU = 0.0002  # налив лимитки — проход за уровень на 0.02% цены


def atr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    tr = np.concatenate([[h[0] - l[0]], tr])
    out = np.empty_like(tr)
    out[:n] = np.nan
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def pivots(h, l, k):
    """Фракталы силы k. Возвращает (ph, pl) — булевы массивы по бару свинга
    (не по бару подтверждения: подтверждение = i + k)."""
    n = len(h)
    ph = np.zeros(n, bool)
    pl = np.zeros(n, bool)
    for i in range(k, n - k):
        wh = h[i - k:i + k + 1]
        wl = l[i - k:i + k + 1]
        if h[i] == wh.max() and (wh == h[i]).sum() == 1:
            ph[i] = True
        if l[i] == wl.min() and (wl == l[i]).sum() == 1:
            pl[i] = True
    return ph, pl


class Series:
    """Массивы одной пары + производные величины."""

    def __init__(self, df):
        self.idx = df.index
        self.o = df['o'].values.astype(float)
        self.h = df['h'].values.astype(float)
        self.l = df['l'].values.astype(float)
        self.c = df['c'].values.astype(float)
        self.v = df['v'].values.astype(float)
        self.tb = df['tb'].values.astype(float)
        self.oi = df['oi'].values.astype(float)
        self.fund = df['funding'].values.astype(float)
        self.fund_rate = self.fund.copy()  # действующая ставка (8 ч) на баре
        self.sig_ok = df['sig_ok'].values.astype(bool)
        self.gap = df['gap'].values.astype(bool)
        self.hour = df.index.hour.values
        self.n = len(df)
        self.atr = atr(self.h, self.l, self.c)
        # медиана объёма за прошлые 48 баров (без текущего)
        import pandas as pd
        self.vmed = pd.Series(self.v).rolling(48, min_periods=24).median().shift(1).values
        self.fund_bar = (self.hour % 8 == 0)
        if 'fund_sum' in df:  # старший ТФ: ставки уже просуммированы по бару
            self.fund = df['fund_sum'].values.astype(float)
            self.fund_bar = np.ones(self.n, bool)


# ───────────────────────────── исполнение ──────────────────────────────

def simulate(S, side, t0, entry, stop, target, expiry, max_hold, market=False):
    """Одна заявка, поставленная на закрытии бара t0.

    side: +1 лонг, −1 шорт. Возвращает dict или None (не налилась).
    """
    h, l, c = S.h, S.l, S.c
    n = S.n
    risk = abs(entry - stop) / entry
    if risk <= 0:
        return None
    fill = None
    if market:
        fill = t0
    else:
        for j in range(t0 + 1, min(n, t0 + 1 + expiry)):
            if side > 0:
                # цель раньше налива — идея ушла без нас
                if h[j] >= target and l[j] > entry * (1 - THRU):
                    return {'filled': False, 'why': 'ran'}
                if l[j] < entry * (1 - THRU):
                    fill = j
                    break
            else:
                if l[j] <= target and h[j] < entry * (1 + THRU):
                    return {'filled': False, 'why': 'ran'}
                if h[j] > entry * (1 + THRU):
                    fill = j
                    break
        if fill is None:
            return {'filled': False, 'why': 'expired'}
    fee_in = TAKER + SLIP_TIME if market else MAKER
    exit_px = None
    why = None
    j_exit = None
    # бар налива: только стоп
    if not market:
        if (side > 0 and l[fill] <= stop) or (side < 0 and h[fill] >= stop):
            exit_px, why, j_exit = stop, 'stop', fill
    if exit_px is None:
        for j in range(fill + 1, min(n, fill + 1 + max_hold)):
            hit_stop = (l[j] <= stop) if side > 0 else (h[j] >= stop)
            hit_tgt = (h[j] > target * (1 + THRU)) if side > 0 else (l[j] < target * (1 - THRU))
            if hit_stop:
                exit_px, why, j_exit = stop, 'stop', j
                break
            if hit_tgt:
                exit_px, why, j_exit = target, 'target', j
                break
        if exit_px is None:
            j_exit = min(n - 1, fill + max_hold)
            exit_px, why = c[j_exit], 'time'
    gross = side * (exit_px - entry) / entry
    if why == 'stop':
        fee_out = TAKER + SLIP_STOP
    elif why == 'target':
        fee_out = MAKER
    else:
        fee_out = TAKER + SLIP_TIME
    # фандинг: платит лонг при положительной ставке
    fsum = 0.0
    for j in range(fill + 1, j_exit + 1):
        if S.fund_bar[j]:
            fsum += S.fund[j]
    funding = -side * fsum
    net = gross - fee_in - fee_out + funding
    return {'filled': True, 'why': why, 'fill': fill, 'exit': j_exit,
            'R': net / risk, 'gross_R': gross / risk, 'cost_R': (fee_in + fee_out) / risk,
            'fund_R': funding / risk, 'risk': risk}


# ───────────────────────────── структура ───────────────────────────────

def liquidity_events(S, k_ext=5, ext_lookback=48, reclaim_bars=2, min_age=6,
                     k_int=2, mss_bars=12):
    """Все снятия внешних пулов с возвратом.

    Возвращает список событий (dict): сторона сделки (+1 после снятия
    лоу = лонг), бар события t (закрытие возврата), уровень пула, экстремум
    снятия, бар начала снятия, «возраст» пула, число пулов, снятых разом,
    бар слома (MSS) если был в пределах mss_bars, и т.д.
    """
    h, l, c, o = S.h, S.l, S.c, S.o
    n = S.n
    ph, pl = pivots(h, l, k_ext)
    iph, ipl = pivots(h, l, k_int)
    # внешний свинг: экстремум за ext_lookback баров до себя
    ext_h = np.zeros(n, bool)
    ext_l = np.zeros(n, bool)
    for i in np.where(ph)[0]:
        a = max(0, i - ext_lookback)
        if h[i] >= h[a:i + 1].max():
            ext_h[i] = True
    for i in np.where(pl)[0]:
        a = max(0, i - ext_lookback)
        if l[i] <= l[a:i + 1].min():
            ext_l[i] = True

    active_hi = []  # [level, bar_of_swing]
    active_lo = []
    broken_hi = []  # [level, swing_bar, first_break_bar]
    broken_lo = []
    last_int_hi = None  # (level, bar) последний подтверждённый внутр. свинг
    last_int_lo = None
    events = []
    for t in range(n):
        # подтверждение свингов, чей бар = t - k
        s = t - k_ext
        if s >= 0:
            if ext_h[s]:
                active_hi.append([h[s], s])
            if ext_l[s]:
                active_lo.append([l[s], s])
        si = t - k_int
        if si >= 0:
            if iph[si]:
                last_int_hi = (h[si], si)
            if ipl[si]:
                last_int_lo = (l[si], si)
        # пробой пулов этим баром
        keep = []
        for lv, sb in active_hi:
            if h[t] > lv and t - sb >= min_age:
                broken_hi.append([lv, sb, t, last_int_lo])
            elif h[t] > lv:
                pass  # слишком молодой пул — просто снят
            else:
                keep.append([lv, sb])
        active_hi = keep
        keep = []
        for lv, sb in active_lo:
            if l[t] < lv and t - sb >= min_age:
                broken_lo.append([lv, sb, t, last_int_hi])
            elif l[t] < lv:
                pass
            else:
                keep.append([lv, sb])
        active_lo = keep
        # возврат за снятый уровень
        nb = []
        fired_short = []
        for item in broken_hi:
            lv, sb, b0, ref = item
            if c[t] < lv:
                fired_short.append(item)
            elif t - b0 < reclaim_bars:
                nb.append(item)
        broken_hi = nb
        nb = []
        fired_long = []
        for item in broken_lo:
            lv, sb, b0, ref = item
            if c[t] > lv:
                fired_long.append(item)
            elif t - b0 < reclaim_bars:
                nb.append(item)
        broken_lo = nb
        for side, fired in ((-1, fired_short), (1, fired_long)):
            if not fired:
                continue
            b0 = min(it[2] for it in fired)
            # самый «старый» пул из снятых разом — главный
            main = min(fired, key=lambda it: it[1])
            lv, sb, _, ref = main
            if side > 0:
                ext = l[b0:t + 1].min()
                ext_bar = b0 + int(np.argmin(l[b0:t + 1]))
            else:
                ext = h[b0:t + 1].max()
                ext_bar = b0 + int(np.argmax(h[b0:t + 1]))
            opp = sorted(p[0] for p in (active_hi if side > 0 else active_lo))
            events.append({
                'side': side, 't': t, 'b0': b0, 'level': lv, 'swing_bar': sb,
                'age': t - sb, 'n_pools': len(fired), 'ext': ext, 'ext_bar': ext_bar,
                'ref': ref,  # внутренний свинг против хода на момент пробоя
                'opp': opp,  # живые пулы с другой стороны — цели
            })
    return events


def find_fvg(S, side, a, b):
    """Имбалансы в сторону side на барах (a, b]. Список (bar, lo, hi)."""
    out = []
    h, l = S.h, S.l
    for j in range(max(a, 2), b + 1):
        if side > 0 and l[j] > h[j - 2]:
            out.append((j, h[j - 2], l[j]))
        if side < 0 and h[j] < l[j - 2]:
            out.append((j, h[j], l[j - 2]))
    return out
