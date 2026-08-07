"""
Скальпинг по RSI и полосам Боллинджера: канон против обратного прочтения.

ЧТО ГОВОРИТ ПРОБНИК (research/rsibb_probe.py, 691 212 пятиминутных баров,
6 пар, ТОЛЬКО бычий период) — и это переворачивает задачу.

    доля случаев, когда цена вернулась к средней линии
    без фильтров                         26%
    RSI 30/70 (канон)                    16%     −10.6 п.п.
    RSI 25/75 (канон, строже)            17%      −9.6 п.п.
    ADX < 25                             26%      −0.2 п.п.
    ADX < 20                             26%      +0.1 п.п.
    RSI 30/70 и ADX < 25                  6%     −20.2 п.п.
    RSI нейтральный 40-60                39%     +12.5 п.п.
    расхождение (лонг при RSI > 45)      55%     +28.9 п.п.
    расхождение (лонг при RSI > 50)      76%     +50.0 п.п.

Три вывода, каждый против учебника.

1. ADX НЕ РАБОТАЕТ ВООБЩЕ. Источники называют его тем, что делает стратегию
   торгуемой («с фильтрами 58-65% попаданий, без них 45%»). На наших данных он
   не двигает исход ни на процент, при любом пороге. Это уже второй случай,
   когда сильное внешнее утверждение с числами не воспроизводится: в замере
   пробоя пересыхание объёма обещало 65% против 48% и дало +0.005 R.

2. КАНОНИЧЕСКИЙ RSI ДЕЛАЕТ ХУЖЕ, А НЕ ЛУЧШЕ. Объяснение простое: RSI на полосе
   помечает не истощение продавца, а действующий импульс — то есть ровно ту
   ходьбу по полосе, от которой фильтр должен был защищать. Каноническая связка
   RSI+ADX даёт 6% возвратов против 26% без всякого отбора: это худшее, что
   можно построить из этих двух индикаторов.

3. ОБРАТНОЕ ПРОЧТЕНИЕ ДАЁТ +50 п.п., И ИМЕННО ПОЭТОМУ ЕМУ НЕЛЬЗЯ ВЕРИТЬ.

ПОДОЗРЕНИЕ ЗАПИСАНО ДО ПРОГОНА, ЧТОБЫ НЕЛЬЗЯ БЫЛО ОБЪЯСНИТЬ ЗАДНИМ ЧИСЛОМ.
Касание нижней полосы — это обычно ТЕНЬ. Если RSI при этом выше 50, значит
закрытия росли, то есть рынок в восходящем тренде, а вниз ушла одна тень.
«Возврат к средней линии» в восходящем тренде — просто продолжение роста.
Тогда «расхождение» отбирает не истощение продавца, а прокол вниз внутри
тренда, и это уже не возврат к среднему, а следование за трендом в другой
одежде. Пробник считался ТОЛЬКО на бычьем периоде, где такое вознаграждается
по построению.

    Если подозрение верно, расхождение развалится на медвежьем периоде
    ЛИБО сохранится, но перекосится в шорты. И то и другое видно в таблице.

АРИФМЕТИКА ИЗДЕРЖЕК ПРОВЕРЕНА И ОНА ПРОХОДИТ. Медианная полуширина канала
0.461% цены, то есть цель на средней линии стоит 0.46%. Круг мейкер-мейкер
0.040% съедает 9% цели, тейкер-тейкер 0.210% — 46%. Вход лимитом на полосе и
цель лимитом на средней — обе заявки мейкерские по построению, поэтому
стратегия арифметически жизнеспособна. Это отличает её от пробоя, где издержки
съедали весь край, но НЕ отличает от сетки в коридоре, где та же арифметика
сходилась, а валовый край всё равно был отрицательным.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА И НЕ ПОДЛЕЖАЩАЯ СМЯГЧЕНИЮ:

    в плюсе на ОБОИХ периодах, доверительный интервал не накрывает ноль
    И просадка не больше 25% на обоих.

РЕЗУЛЬТАТ НА ПЯТИМИНУТКАХ: ВСЕ 17 ВАРИАНТОВ ОТРИЦАТЕЛЬНЫ НА ОБОИХ ПЕРИОДАХ.

    вариант                        бык      медведь    просадка
    канон RSI 30/70              −0.700     −0.768     100% / 100%
    канон + ADX < 20             −0.863     −0.927     100% / 100%
    без RSI вообще               −0.429     −0.452     100% / 100%
    расхождение RSI > 50         −0.056     −0.111      32% /  44%
    расхождение · стоп 1.0       −0.021     −0.065      25% /  29%

ПОДТВЕРЖДЕНО, ЧТО КАНОН НЕ ПРОСТО НЕ РАБОТАЕТ, А ВРЕДИТ. Без RSI −0.43, с
каноническим RSI −0.70, с добавленным ADX −0.86. Чем ближе к учебнику, тем
хуже, и это согласуется с пробником, где канон давал 6% возвратов к средней
против 26% без всякого отбора.

ЗАПИСАННОЕ ПОДОЗРЕНИЕ ПРО РАСХОЖДЕНИЕ ОКАЗАЛОСЬ ВЕРНЫМ НАПОЛОВИНУ. Оно не
развалилось на медведе и по сторонам почти не перекошено (54-57% лонгов), но
край на медведе вдвое слабее бычьего (0.037 против 0.083) — примерно столько и
объясняется трендовой составляющей.

ГЛАВНОЕ ЧИСЛО ЗАМЕРА НЕ В КОЛОНКЕ РЕЗУЛЬТАТА, А РЯДОМ. У варианта
«расхождение · стоп 1.0» валовый край ПОЛОЖИТЕЛЕН на обоих периодах: +0.083 и
+0.037. Издержки при этом 0.104 R и съедают его целиком. То есть край в идее
есть, он просто меньше стоимости круга комиссий.

Отсюда и появилась ось таймфрейма выше: издержки в R равны «круг ÷ стоп», а
стоп пропорционален ширине канала. Это единственная величина в формуле,
которой можно управлять, не трогая саму идею.

РЕЗУЛЬТАТ НА ЧАСЕ: ПРЕДСКАЗАНИЕ ПО ИЗДЕРЖКАМ ПОДТВЕРДИЛОСЬ ТОЧНО.

    «расхождение · стоп 1.0»    издержки   валовый край   чистый
    5m, 8 пар                     0.104    +0.083/+0.037  −0.021/−0.065
    1h, 8 пар                     0.061    +0.094/+0.068  +0.033/+0.002
    1h, 20 пар                    0.058    +0.083/+0.112  +0.025/+0.050

Издержки упали почти вдвое, как и считалось по тождеству «круг ÷ стоп», а
валовый край смену масштаба пережил. Чистый результат перешёл через ноль и
остался положительным на обоих периодах при обоих размерах пула.

ПРИЁМКУ ЭТО НЕ ПРОШЛО: интервалы [−0.049; +0.100] и [−0.028; +0.124] накрывают
ноль. Выборка выросла вдвое (728 и 723 сделки), интервал сузился с ±0.11 до
±0.075 — не хватило.

ЧТО ЗДЕСЬ ВАЖНО НЕ ПЕРЕПУТАТЬ. Медвежья оценка выросла с +0.002 до +0.050 при
удвоении выборки. Значит прежний «ноль» был недобором данных, а не нулём, и
знак края устойчив: положителен на двух периодах при двух размерах пула и двух
таймфреймах. Это не то же самое, что «один вариант из семнадцати случайно
оказался в плюсе».

НО И ПЕРЕОЦЕНИВАТЬ НЕЛЬЗЯ. Сам вариант «стоп 1.0 полуширины» найден перебором,
а не назначен заранее; заранее было предсказано и подтверждено только поведение
ИЗДЕРЖЕК. При семнадцати вариантах на четырёх прогонах одна пограничная клетка
ожидаема и без всякого края.

ЧТО ГОВОРИТ ПРО САМ УЧЕБНИК. Канон отвергнут окончательно и на всех масштабах:
−0.70 и −0.78 на часе при просадке 100%, а с ADX < 20 — −0.87 и −1.05, худшее
в таблице. Без RSI результат ЛУЧШЕ, чем с ним. Работает только обратное
прочтение: покупка нижней полосы при НЕслабом импульсе.

ИМЯ ФАЙЛА НЕ rsibb.py СОЗНАТЕЛЬНО. Каталог замеров стоит в пути раньше
Live_Bot, и файл, названный как пакет стратегии, заслонил бы его собой:
`from rsibb import core` нашёл бы этот самый скрипт.

Запуск:
    python research/bollinger_rsi.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = int(os.getenv('RSIBB_PAIRS', 8))

# ТАЙМФРЕЙМ — ЭТО ОСЬ ИЗДЕРЖЕК, А НЕ НАСТРОЙКА УДОБСТВА, и первый прогон это
# показал числом. Результат считается в единицах риска, риск задаёт СТОП, а
# стоп пропорционален ширине канала. Отсюда:
#
#     издержки в R = круг комиссий % / стоп %
#
# На пятиминутках стоп упирался в пол 0.40%, и круг мейкер-мейкер 0.040% стоил
# 0.10 R, а с фандингом 0.14-0.16 R. При этом лучший вариант дал валовый край
# +0.083 на быке и +0.037 на медведе — то есть край ЕСТЬ, он просто меньше
# стоимости круга.
#
# На часовом графике полосы в разы шире, стоп выходит около 1-1.5%, и тот же
# круг обходится в 0.03-0.04 R. Ровно это и проверяется: тот же код, те же
# варианты, другой масштаб издержек.
TIMEFRAME = os.getenv('RSIBB_TF', '5m')
BAR_MIN = {'5m': 5, '15m': 15, '1h': 60, '4h': 240}[TIMEFRAME]

BASE = {
    'rsi_mode': 'extreme', 'rsi_low': 30.0, 'rsi_high': 70.0,
    'adx_max': 0.0, 'max_width_ratio': 0.0,
    'entry_mode': 'touch', 'target_frac': 1.0, 'stop_frac': 0.5,
    'thin_stop': 'widen',
}

VARIANTS = [
    # ── Канон, как в учебнике ──────────────────────────────────────────────
    ('канон: RSI 30/70, цель на средней',      {}),
    ('канон строже: RSI 25/75',                {'rsi_low': 25, 'rsi_high': 75}),
    ('канон + ADX < 25',                       {'adx_max': 25}),
    ('канон + ADX < 20',                       {'adx_max': 20}),

    # ── Контроль: а нужен ли RSI вообще ────────────────────────────────────
    ('без RSI — только полоса',                {'rsi_mode': 'off'}),
    ('без RSI + ADX < 25',                     {'rsi_mode': 'off',
                                                'adx_max': 25}),

    # ── Обратное прочтение, к которому есть записанное подозрение ──────────
    ('расхождение: лонг при RSI > 45',         {'rsi_mode': 'divergence',
                                                'rsi_low': 45, 'rsi_high': 55}),
    ('расхождение сильнее: RSI > 50',          {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50}),
    ('нейтральный RSI 40-60',                  {'rsi_mode': 'neutral',
                                                'rsi_low': 40, 'rsi_high': 60}),

    # ── Геометрия выхода ───────────────────────────────────────────────────
    ('расхождение · цель на дальней полосе',   {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'target_frac': 2.0}),
    ('расхождение · стоп 1.0 полуширины',      {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'stop_frac': 1.0}),
    ('расхождение · вход после возврата',      {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'entry_mode': 'reclaim'}),
    ('канон · вход после возврата',            {'entry_mode': 'reclaim'}),

    # ── Фильтр расширения полос ────────────────────────────────────────────
    ('расхождение · полосы не расширяются',    {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'max_width_ratio': 1.1}),

    # ── Узкий канал: расширять стоп или не брать сетап ─────────────────────
    # Первый прогон показал, что пол по стопу упирается ПОЧТИ ВСЕГДА (медиана
    # ровно 0.40 — само значение пола). Значит канон мерился с отношением
    # риска к прибыли 1.15 вместо задуманных 2.0. Это развилка реализации, а
    # не результат, и она проверяется, а не назначается.
    ('канон · узкий канал не брать',           {'thin_stop': 'skip'}),
    ('расхождение · узкий канал не брать',     {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'thin_stop': 'skip'}),
    ('без RSI · узкий канал не брать',         {'rsi_mode': 'off',
                                                'thin_stop': 'skip'}),
]


def scan(pairs, cache_dir, label):
    """Индикаторы всех пар. Считаются один раз на период."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    from rsibb import core

    print(f'[{label}] загрузка и индикаторы...', flush=True)
    data, marks = {}, []
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or TIMEFRAME not in loaded:
            continue
        df = loaded[TIMEFRAME]
        data[pair] = df
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        marks.append({
            'pair': pair,
            'ind': core.indicators(df['open'].to_numpy(float),
                                   df['high'].to_numpy(float),
                                   df['low'].to_numpy(float),
                                   df['close'].to_numpy(float)),
            'stamps': stamps.to_numpy(),
        })
        print(f'      {pair}: баров {len(df)}', flush=True)
    return data, marks


def build_orders(marks, cfg):
    from rsibb import core, params
    from smc_engine import Order

    life = np.timedelta64(params.EXPIRY_BARS * BAR_MIN * 60, 's')
    orders, reasons = [], {}
    for mark in marks:
        ind, stamps = mark['ind'], mark['stamps']
        n = len(ind['close'])
        # Пауза между сигналами одной пары: в боковике условие держится
        # десятками баров подряд, и без неё замер посчитал бы один и тот же
        # заход сто раз, раздув и число сделок, и уверенность в результате.
        last = -10 ** 9
        for i in range(60, n - 2):
            if i - last < params.MAX_HOLD_BARS // 4:
                continue
            setup, why = core.evaluate(
                ind, i, rsi_low=cfg['rsi_low'], rsi_high=cfg['rsi_high'],
                adx_max=cfg['adx_max'], max_width_ratio=cfg['max_width_ratio'],
                entry_mode=cfg['entry_mode'], rsi_mode=cfg['rsi_mode'])
            if setup is None:
                key = why.split('(')[0].split(' — ')[0][:34]
                reasons[key] = reasons.get(key, 0) + 1
                continue
            trade = core.build_trade(setup, target_frac=cfg['target_frac'],
                                     stop_frac=cfg['stop_frac'],
                                     thin_stop=cfg['thin_stop'])
            if trade is None:
                reasons['геометрия не годится'] = reasons.get(
                    'геометрия не годится', 0) + 1
                continue
            last = i
            created = stamps[i]
            orders.append(Order(
                pair=mark['pair'], direction=setup['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=[trade['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(mark['pair'], i),
                entry_type='limit' if cfg['entry_mode'] == 'touch' else 'stop',
                meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                      'rsi': setup['rsi'], 'adx': setup['adx'],
                      'direction': setup['direction']},
            ))
    return orders, reasons


def run(marks, data, cfg):
    from rsibb import params
    from smc_engine import compute_stats, run_portfolio

    orders, _reasons = build_orders(marks, cfg)
    if len(orders) < 20:
        return None
    result = run_portfolio(
        orders, data, risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 20:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    longs = sum(1 for o in orders if o.direction == 'LONG')
    stop_pct = np.array([(o.meta or {}).get('stop_pct', 0) for o in orders], float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'fill': len(trades) / len(orders) * 100,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'longs': longs / len(orders) * 100,
            'stop_pct': float(np.median(stop_pct))}


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = scan(pairs, cache, label)

    results = {}
    for label, (data, marks) in periods.items():
        print()
        print('=' * 124)
        print(f'{label}   пар: {len(data)}')
        print('=' * 124)
        head = (f'{"вариант":<40}{"заявок":>8}{"сделок":>8}{"налив":>7}'
                f'{"стоп %":>8}{"лонг":>6}{"винрейт":>9}{"R вал.":>9}'
                f'{"издер.":>8}{"R/сделку":>10}{"сумма":>8}{"DD%":>7}'
                f'{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, override in VARIANTS:
            cfg = dict(BASE, **override)
            res = run(marks, data, cfg)
            if res is None:
                print(f'{name:<40}{"— мало сделок":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<40}{res["orders"]:>8}{res["n"]:>8}{res["fill"]:>6.0f}%'
                  f'{res["stop_pct"]:>8.2f}{res["longs"]:>5.0f}%'
                  f'{res["wr"]:>8.1f}%{res["gross"]:>9.3f}{res["costs"]:>8.3f}'
                  f'{res["mean"]:>10.3f}{res["total"]:>8.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 124)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ОБОИХ периодах,')
    print('интервал не накрывает ноль И просадка не больше 25% на обоих.')
    print('=' * 124)
    for name, _ in VARIANTS:
        cells, ok = '', []
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>36}'
                ok.append(False)
                continue
            lo, hi = ci(res['r'])
            ok.append(res['mean'] > 0 and lo > 0 and res['dd'] <= 25)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}] DD {res["dd"]:.0f}%'
            cells += f'{cell:>36}'
        print(f'{name:<40}{cells}{"  ПРИНЯТ" if ok and all(ok) else ""}')

    print()
    print('ПРОВЕРКА ЗАПИСАННОГО ПОДОЗРЕНИЯ. Если «расхождение» держится только')
    print('на быке или резко перекошено по стороне (колонка «лонг»), значит')
    print('оно отбирает прокол внутри тренда, а не истощение движения, и')
    print('называть это возвратом к среднему нельзя. Смотреть надо на две')
    print('колонки сразу: результат по периодам и долю лонгов.')


if __name__ == '__main__':
    main()
