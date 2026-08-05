"""
Честный портфельный движок бэктеста.

Отличия от research/backtest.py, которые меняют выводы, а не косметику:

1. ЛИМИТ ИСПОЛНЯЕТСЯ, ТОЛЬКО ЕСЛИ ЦЕНА ДО НЕГО ДОШЛА.
   Старый движок брал цену входа из сигнала и сразу считал сделку открытой,
   хотя вход стоит на уровне, которого рынок ещё не достиг. Часть таких
   сделок в реальности не открылась бы вовсе — а в модели они приносили
   прибыль. Здесь ордер живёт заданное время и отменяется, если не налит.

2. КОМИССИИ. Старый движок считал PnL без единой комиссии. При 3 частичных
   тейках это заметная недооценка издержек.

3. ДЕДУПЛИКАЦИЯ СИГНАЛОВ. Генератор выдаёт сетап на каждой свече, пока цена
   идёт к зоне. Это один торговый сетап, а не сотня: ключуем по зоне.

Движок стратегия-агностичен: принимает список ордеров и симулирует портфель
одинаково и для SMC, и для старой фибо-стратегии — только так сравнение
двух стратегий имеет смысл.
"""

from datetime import timedelta

import numpy as np
import pandas as pd

# ── Издержки (Bybit linear perpetual, VIP0) ─────────────────────────────────
FEE_MAKER = 0.0002    # 0.02% — лимитный вход и лимитные тейки
FEE_TAKER = 0.00055   # 0.055% — стоп исполняется по рынку

# Проскальзывание рыночных выходов (стоп, тайм-стоп). Стоп-маркет почти
# никогда не исполняется ровно по своей цене: он срабатывает на движении,
# и цена уходит дальше. При винрейте около 25% стопы — самое частое событие
# в системе, поэтому допущение «стоп ровно по цене» ощутимо завышает результат.
# Задаётся долей цены; 0 отключает.
SLIPPAGE_PCT = 0.0005   # 0.05%

# Фандинг за удержание позиции на бессрочных фьючерсах. Типичная ставка на
# Bybit — 0.01% каждые 8 часов, то есть 0.03% в сутки от НОМИНАЛА позиции.
#
# Почему это нельзя игнорировать именно здесь: при риске 1% и стопе 1% номинал
# позиции равен всему депозиту. Стратегия с дальними целями держит сделку по
# несколько дней, и набегает порядка 0.09R на сделку — при преимуществе
# 0.397R это четверть всего эджа. Стратегию с длинным удержанием такой расчёт
# штрафует сильнее, чем скальпирующую, и это честно: она действительно платит
# больше.
#
# Знак фандинга зависит от перекоса рынка и может быть в пользу позиции;
# берём среднюю величину как расход для обеих сторон — консервативно.
FUNDING_PCT_PER_DAY = 0.0003

INITIAL_BALANCE = 10_000.0


class Order:
    """Отложенный лимитный ордер, ожидающий налива."""

    __slots__ = ('pair', 'direction', 'entry', 'stop', 'targets', 'fractions',
                 'created', 'expires', 'key', 'meta', 'be_trigger',
                 'entry_type', 'trail_distance')

    def __init__(self, pair, direction, entry, stop, targets, fractions,
                 created, expires, key, meta=None, be_trigger=None,
                 entry_type='limit', trail_distance=None):
        self.pair = pair
        self.direction = direction
        self.entry = entry
        self.stop = stop
        self.targets = list(targets)
        self.fractions = list(fractions)
        self.created = created
        self.expires = expires
        self.key = key
        self.meta = meta or {}
        # Цена, при достижении которой стоп переводится в безубыток.
        # Текущая v3 использует для этого уровень B импульса, SMC — первый
        # тейк. Без поддержки этого механизма старая стратегия в сравнении
        # выглядела бы хуже, чем она есть.
        self.be_trigger = be_trigger
        # 'limit' — цена ПРИХОДИТ к уровню (возвратные стратегии: вход в зону).
        # 'stop'  — цена УХОДИТ за уровень (пробойные: вход по направлению).
        # Разница принципиальна для симуляции: лимит на уровне выше рынка
        # налился бы мгновенно и по лучшей цене, чем в реальности, а стоп на
        # том же уровне ждёт, пока рынок туда дойдёт. Без разделения пробойная
        # стратегия получала бы вход по цене, которой на рынке не было.
        self.entry_type = entry_type
        # Дистанция трейлинг-стопа в единицах цены. None — трейлинга нет.
        # Нужна трендовым стратегиям: их прибыль в редких длинных движениях,
        # а фиксированная цель обрезает ровно тот хвост, ради которого всё и
        # затевается. Стоп только подтягивается, никогда не ослабляется.
        self.trail_distance = trail_distance


def to_naive_ns(series):
    """
    Метки времени как наивный datetime64[ns] в UTC.

    to_numpy() на tz-aware серии отдаёт object-массив pandas.Timestamp, и
    numpy-сравнения с datetime64 падают. Снимаем таймзону явно — все данные
    и так в UTC, поэтому смысл не теряется.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(series, utc=True))
    return idx.tz_convert('UTC').tz_localize(None).to_numpy(dtype='datetime64[ns]')


def _prepare(df):
    """Массивы numpy для быстрой симуляции (iterrows слишком медленный)."""
    return {
        'ts': to_naive_ns(df['timestamp']),
        'open': df['open'].to_numpy(dtype=float),
        'high': df['high'].to_numpy(dtype=float),
        'low': df['low'].to_numpy(dtype=float),
        'close': df['close'].to_numpy(dtype=float),
    }


def simulate_order(order, exec_arrays, start_pos, risk_amount,
                   breakeven_after_tp1=True, max_hold_hours=336.0):
    """
    Проводит один ордер через свечи исполнения: ожидание налива, затем ведение
    позиции до выхода.

    Возвращает dict результата или None, если ордер истёк не налившись.

    Внутрисвечная неоднозначность (в одной свече задеты и стоп, и тейк)
    решается консервативно — считаем, что первым сработал стоп. Обратное
    допущение систематически завышает результат.
    """
    ts = exec_arrays['ts']
    opens = exec_arrays['open']
    high = exec_arrays['high']
    low = exec_arrays['low']
    close = exec_arrays['close']
    size = len(ts)

    is_long = order.direction in ('BULLISH', 'LONG')
    is_stop_entry = getattr(order, 'entry_type', 'limit') == 'stop'

    # ── Фаза 1: ждём налива ──────────────────────────────────────────────
    fill_pos = None
    for i in range(start_pos, size):
        if ts[i] > order.expires:
            break
        if is_stop_entry:
            touched = high[i] >= order.entry if is_long else low[i] <= order.entry
        else:
            touched = low[i] <= order.entry if is_long else high[i] >= order.entry
        if touched:
            fill_pos = i
            break

    if fill_pos is None:
        return None   # ордер не налился — сделки не было

    entry = order.entry
    if is_stop_entry:
        # Разрыв через уровень: стоп-маркет исполняется по открытию свечи,
        # а не по своей цене. Считать иначе значило бы дарить стратегии
        # лучшую цену ровно в тех случаях, когда рынок ушёл против неё.
        gapped = opens[fill_pos] > entry if is_long else opens[fill_pos] < entry
        if gapped:
            entry = opens[fill_pos]
    entry_time = ts[fill_pos]
    sl_distance = abs(entry - order.stop)
    if sl_distance <= 0:
        return None

    position = risk_amount / sl_distance
    notional_in = position * entry
    # Вход по стопу исполняется по рынку — комиссия тейкерская, а не мейкерская.
    fees = notional_in * (FEE_TAKER if is_stop_entry else FEE_MAKER)

    remaining = position
    realised = 0.0
    stop = order.stop
    tp_hit = 0
    deadline = entry_time + np.timedelta64(int(max_hold_hours * 3600), 's')

    # MFE/MAE — максимальный ход в пользу и против позиции, в единицах риска.
    # Нужны для диагностики: если убыточные сделки успевают дойти до 1R,
    # значит проблема в слишком далёкой первой цели, а не во входах.
    best_price = entry
    worst_price = entry

    # Фандинг начисляем ПОСВЕЧНО, а не по общему времени удержания: после
    # частичной фиксации номинал позиции уменьшается, и платить за него
    # полную ставку было бы неверно.
    funding = 0.0
    bar_days = 0.0
    if size > 1:
        bar_ns = float(ts[1] - ts[0])
        bar_days = bar_ns / (24 * 3600 * 1e9)

    # ── Фаза 2: ведение позиции ──────────────────────────────────────────
    be_armed = False

    # Свеча НАЛИВА проверяется наравне с остальными. Раньше цикл начинался со
    # следующей, и сделка, которую та же свеча уносила на стоп, получала
    # бесплатный шанс: на реальных данных это 2.4% сделок, каждая ценой около
    # 1R. Порядок событий внутри свечи неизвестен, поэтому трактуем против
    # себя — как и везде в этом движке.
    for i in range(fill_pos, size):
        hi, lo = high[i], low[i]

        # Трейлинг считается по лучшей цене на КОНЕЦ ПРЕДЫДУЩЕЙ свечи, до
        # того как в best_price попадёт экстремум текущей. Иначе стоп внутри
        # одной свечи подтягивался бы к её же максимуму и выбивал позицию по
        # цене, до которой рынок на момент срабатывания ещё не дошёл, —
        # классическое подглядывание внутрь свечи, дающее красивую кривую и
        # невоспроизводимое вживую.
        trail = order.trail_distance
        if trail:
            trailed = best_price - trail if is_long else best_price + trail
            stop = max(stop, trailed) if is_long else min(stop, trailed)

        if is_long:
            best_price = max(best_price, hi)
            worst_price = min(worst_price, lo)
        else:
            best_price = min(best_price, lo)
            worst_price = max(worst_price, hi)

        funding += remaining * close[i] * FUNDING_PCT_PER_DAY * bar_days

        # Безубыток по триггерной цене (уровень B у фибо-стратегии)
        if order.be_trigger is not None and not be_armed:
            reached = hi >= order.be_trigger if is_long else lo <= order.be_trigger
            if reached:
                stop = entry
                be_armed = True

        target = order.targets[tp_hit] if tp_hit < len(order.targets) else None
        sl_touched = lo <= stop if is_long else hi >= stop
        tp_touched = target is not None and (hi >= target if is_long else lo <= target)

        # Консервативно: при одновременном касании считаем сработавшим стоп
        if sl_touched:
            # Стоп-маркет проскальзывает против позиции
            fill = stop * (1 - SLIPPAGE_PCT) if is_long else stop * (1 + SLIPPAGE_PCT)
            pnl = remaining * ((fill - entry) if is_long else (entry - fill))
            realised += pnl
            fees += remaining * fill * FEE_TAKER
            return {
                'pair': order.pair,
                'direction': order.direction,
                'entry_time': entry_time,
                'exit_time': ts[i],
                'entry': entry,
                'exit': fill,
                'stop': order.stop,
                'pnl': realised - fees - funding,
                'funding': funding,
                'gross_pnl': realised,
                'fees': fees,
                'tps_hit': tp_hit,
                'exit_reason': f'SL_after_TP{tp_hit}' if tp_hit else 'SL',
                'position': position,
                'risk': risk_amount,
                'meta': order.meta,
                'mfe_r': abs(best_price - entry) / sl_distance,
                'mae_r': abs(worst_price - entry) / sl_distance,
            }

        # Свеча может пройти СКВОЗЬ несколько целей сразу. Оба лимитных тейка
        # стоят в стакане, и такая свеча исполняет оба — брать по одной цели
        # за свечу значило бы держать позицию, которой уже нет, и терять
        # фиксацию, если цена не вернётся.
        # Стоп проверен выше и не сработал, поэтому здесь безопасно.
        while tp_touched:
            fraction = order.fractions[tp_hit]
            closed = position * fraction
            closed = min(closed, remaining)
            pnl = closed * ((target - entry) if is_long else (entry - target))
            realised += pnl
            fees += closed * target * FEE_MAKER
            remaining -= closed
            tp_hit += 1

            if breakeven_after_tp1 and tp_hit == 1:
                stop = entry   # §14.1: безубыток после первой фиксации

            if remaining > 1e-12 and tp_hit < len(order.targets):
                target = order.targets[tp_hit]
                tp_touched = (hi >= target) if is_long else (lo <= target)
                if tp_touched:
                    continue

            if remaining <= 1e-12 or tp_hit >= len(order.targets):
                return {
                    'pair': order.pair,
                    'direction': order.direction,
                    'entry_time': entry_time,
                    'exit_time': ts[i],
                    'entry': entry,
                    'exit': target,
                    'stop': order.stop,
                    'pnl': realised - fees - funding,
                    'funding': funding,
                    'gross_pnl': realised,
                    'fees': fees,
                    'tps_hit': tp_hit,
                    'exit_reason': f'TP{tp_hit}',
                    'position': position,
                    'risk': risk_amount,
                    'meta': order.meta,
                    'mfe_r': abs(best_price - entry) / sl_distance,
                    'mae_r': abs(worst_price - entry) / sl_distance,
                }

        if ts[i] >= deadline:
            # Тайм-стоп закрывается по рынку — тоже с проскальзыванием
            raw = close[i]
            price = raw * (1 - SLIPPAGE_PCT) if is_long else raw * (1 + SLIPPAGE_PCT)
            pnl = remaining * ((price - entry) if is_long else (entry - price))
            realised += pnl
            fees += remaining * price * FEE_TAKER
            return {
                'pair': order.pair,
                'direction': order.direction,
                'entry_time': entry_time,
                'exit_time': ts[i],
                'entry': entry,
                'exit': price,
                'stop': order.stop,
                'pnl': realised - fees - funding,
                'funding': funding,
                'gross_pnl': realised,
                'fees': fees,
                'tps_hit': tp_hit,
                'exit_reason': 'TIME_STOP',
                'position': position,
                'risk': risk_amount,
                'meta': order.meta,
                'mfe_r': abs(best_price - entry) / sl_distance,
                'mae_r': abs(worst_price - entry) / sl_distance,
            }

    # Данные кончились — закрываем по последней цене
    raw = close[-1]
    price = raw * (1 - SLIPPAGE_PCT) if is_long else raw * (1 + SLIPPAGE_PCT)
    pnl = remaining * ((price - entry) if is_long else (entry - price))
    realised += pnl
    fees += remaining * price * FEE_TAKER
    return {
        'pair': order.pair,
        'direction': order.direction,
        'entry_time': entry_time,
        'exit_time': ts[-1],
        'entry': entry,
        'exit': price,
        'stop': order.stop,
        'pnl': realised - fees - funding,
        'funding': funding,
        'gross_pnl': realised,
        'fees': fees,
        'tps_hit': tp_hit,
        'exit_reason': 'EOD',
        'position': position,
        'risk': risk_amount,
        'meta': order.meta,
        'mfe_r': abs(best_price - entry) / sl_distance,
        'mae_r': abs(worst_price - entry) / sl_distance,
    }


def run_portfolio(orders, exec_data, risk_pct=1.0, max_positions=5,
                  cooldown_hours=12.0, initial_balance=INITIAL_BALANCE,
                  breakeven_after_tp1=True, max_hold_hours=336.0,
                  max_same_direction=0, risk_scale=None):
    """
    Портфельная симуляция: ордера в хронологическом порядке, ограничения по
    числу позиций и кулдауну, риск считается от ТЕКУЩЕГО баланса.

    orders    — список Order, отсортируется по created
    exec_data — {pair: DataFrame свечей исполнения (5m)}
    risk_scale — необязательная функция order -> множитель риска. Нужна, чтобы
        проверять переменный размер позиции, не трогая сам сигнал: R-множитель
        сделки от размера не зависит, а доходность и просадка зависят, и без
        этого крючка отличить «торговать меньше» от «не торговать» нельзя.
        Множитель обязан считаться ТОЛЬКО по прошлым данным на момент
        order.created — иначе в симуляцию попадёт будущее.
    """
    prepared = {pair: _prepare(df) for pair, df in exec_data.items()}
    positions = {}
    for pair, arrays in prepared.items():
        positions[pair] = arrays['ts']

    orders = sorted(orders, key=lambda o: o.created)

    balance = initial_balance
    equity_curve = [(orders[0].created if orders else None, balance)]
    trades = []
    active = {}     # pair -> exit_time
    active_dir = {} # pair -> (exit_time, направление) для направленного кэпа
    cooldown = {}   # pair -> время окончания кулдауна
    seen_keys = set()
    skipped = {'duplicate': 0, 'active': 0, 'cooldown': 0, 'capacity': 0,
               'same_direction': 0, 'no_fill': 0}

    for order in orders:
        if order.key in seen_keys:
            skipped['duplicate'] += 1
            continue

        created = order.created
        if order.pair in active and active[order.pair] > created:
            skipped['active'] += 1
            continue
        if order.pair in cooldown and cooldown[order.pair] > created:
            skipped['cooldown'] += 1
            continue

        open_now = sum(1 for exit_time in active.values() if exit_time > created)
        if open_now >= max_positions:
            skipped['capacity'] += 1
            continue

        # Направленный кэп: несколько позиций в одну сторону на криптопарах —
        # это не диверсификация, а одна ставка с умноженным риском. Все
        # альткоины ходят за биткоином, и в коррекции такие позиции гибнут
        # вместе. При низком винрейте это главный источник глубокой просадки.
        if max_same_direction:
            same = sum(1 for exit_time, direction in active_dir.values()
                       if exit_time > created and direction == order.direction)
            if same >= max_same_direction:
                skipped['same_direction'] += 1
                continue

        arrays = prepared.get(order.pair)
        if arrays is None:
            continue
        start_pos = int(np.searchsorted(arrays['ts'], np.datetime64(created), side='right'))
        if start_pos >= len(arrays['ts']):
            continue

        scale = 1.0 if risk_scale is None else float(risk_scale(order))
        if scale <= 0:
            skipped['risk_zero'] = skipped.get('risk_zero', 0) + 1
            continue
        risk_amount = balance * risk_pct / 100 * scale
        result = simulate_order(
            order, arrays, start_pos, risk_amount,
            breakeven_after_tp1=breakeven_after_tp1,
            max_hold_hours=max_hold_hours,
        )

        # Ключ помечаем использованным независимо от исхода: зона отработана
        seen_keys.add(order.key)

        if result is None:
            skipped['no_fill'] += 1
            continue

        balance += result['pnl']
        result['balance'] = balance
        result['risk_scale'] = scale
        result['pnl_pct'] = result['pnl'] / (balance - result['pnl']) * 100
        trades.append(result)
        equity_curve.append((result['exit_time'], balance))

        active[order.pair] = result['exit_time']
        active_dir[order.pair] = (result['exit_time'], order.direction)
        cooldown[order.pair] = result['exit_time'] + np.timedelta64(
            int(cooldown_hours * 3600), 's')

    return {'trades': trades, 'equity': equity_curve,
            'balance': balance, 'skipped': skipped}


def compute_stats(result, initial_balance=INITIAL_BALANCE, label=''):
    """Сводная статистика прогона."""
    trades = result['trades']
    if not trades:
        return {'label': label, 'trades': 0, 'return_pct': 0.0, 'max_dd_pct': 0.0,
                'winrate': 0.0, 'profit_factor': 0.0, 'expectancy_r': 0.0,
                'fees': 0.0, 'skipped': result['skipped']}

    pnls = np.array([t['pnl'] for t in trades], dtype=float)
    risks = np.array([t['risk'] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    balances = np.array([initial_balance] + [t['balance'] for t in trades], dtype=float)
    peak = np.maximum.accumulate(balances)
    drawdown = (peak - balances) / peak
    r_multiples = pnls / np.where(risks > 0, risks, np.nan)

    gross_win = wins.sum()
    gross_loss = abs(losses.sum())

    return {
        'label': label,
        'trades': len(trades),
        'return_pct': (result['balance'] / initial_balance - 1) * 100,
        'final_balance': result['balance'],
        'max_dd_pct': float(drawdown.max() * 100),
        'winrate': float(len(wins) / len(pnls) * 100),
        'profit_factor': float(gross_win / gross_loss) if gross_loss > 0 else float('inf'),
        'expectancy_r': float(np.nanmean(r_multiples)),
        'sum_r': float(np.nansum(r_multiples)),
        'avg_win': float(wins.mean()) if len(wins) else 0.0,
        'avg_loss': float(losses.mean()) if len(losses) else 0.0,
        'fees': float(sum(t['fees'] for t in trades)),
        'skipped': result['skipped'],
    }


def print_stats(stats_list):
    """Печатает сравнительную таблицу нескольких прогонов."""
    rows = [
        ('Сделок', 'trades', '{:>10.0f}'),
        ('Доходность %', 'return_pct', '{:>10.1f}'),
        ('Макс. просадка %', 'max_dd_pct', '{:>10.1f}'),
        ('Винрейт %', 'winrate', '{:>10.1f}'),
        ('Profit factor', 'profit_factor', '{:>10.3f}'),
        ('Матожидание, R', 'expectancy_r', '{:>10.3f}'),
        ('Сумма R', 'sum_r', '{:>10.1f}'),
        ('Комиссии $', 'fees', '{:>10.0f}'),
    ]

    width = 22
    header = 'Метрика'.ljust(width) + ''.join(f'{s["label"]:>12}' for s in stats_list)
    print('\n' + header)
    print('-' * len(header))
    for title, key, fmt in rows:
        line = title.ljust(width)
        for stats in stats_list:
            value = stats.get(key, 0)
            line += f'{fmt.format(value):>12}' if isinstance(value, (int, float)) else f'{value:>12}'
        print(line)

    print('\nОтсев ордеров:')
    for stats in stats_list:
        skipped = stats.get('skipped', {})
        parts = ', '.join(f'{k}={v}' for k, v in skipped.items())
        print(f'   {stats["label"]}: {parts}')
