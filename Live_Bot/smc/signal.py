"""
Генератор торгового сетапа — сборка всех слоёв SMC (§23, итоговый чек-лист).

Порядок принятия решения повторяет методичку сверху вниз (§2.6):

    1. Bias старшего ТФ (1D/4H) — структура, а не индикатор. Направление
       сделки обязано ему соответствовать (§2.6: 80% сделок по тренду).
    2. Зона интереса на рабочем ТФ, обязательно в дисконте для лонга или
       в премиуме для шорта относительно equilibrium (§10.1).
    3. Подтверждение зоны снятием ликвидности (§5, §23).
    4. Confluence-скор из независимых факторов; торгуем только сетапы,
       набравшие MIN_CONFLUENCE_SCORE.
    5. Геометрия сделки: вход в зоне, стоп за её экстремумом, цели по
       расширениям Фибоначчи и непротестированным пулам ликвидности.
    6. Гейт по RR (§16: минимум 1:3) и расчёт размера позиции (§15.2).

MarketContext считает тяжёлую часть (структуры, POI, свипы, имбалансы) ОДИН
раз на пару, после чего evaluate() на каждой свече стоит дёшево. Без этого
бэктест на 16 парах за год не считался бы за разумное время.
"""

from bisect import bisect_left, bisect_right

import numpy as np
import pandas as pd

from . import (fib, imbalance, liquidity, params, poi as poi_mod,
               sessions, structure as structure_mod)

BULLISH = 'BULLISH'
BEARISH = 'BEARISH'
NEUTRAL = 'NEUTRAL'


def to_ns(values):
    """
    Метки времени в наносекундах — с ЯВНЫМ приведением разрешения.

    pandas 3 хранит datetime64 в том разрешении, в котором колонка была
    создана: pd.to_datetime(..., unit='ms') даёт datetime64[ms], и наивный
    .astype('int64') вернёт миллисекунды, а Timestamp.value — всегда
    наносекунды. Сравнение таких чисел молча ломает связь таймфреймов
    (bias начинает читаться из конца истории). Поэтому as_unit('ns') здесь
    обязателен, а не косметика.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(values, utc=True)).as_unit('ns')
    return idx.asi8


def bar_duration_ns(df):
    """
    Длительность свечи фрейма в наносекундах (по медиане интервалов).

    Медиана, а не первая разница: в данных бирж встречаются пропуски свечей,
    и одиночный разрыв не должен исказить оценку таймфрейма.
    """
    stamps = df['_ts_ns'].to_numpy() if '_ts_ns' in df.columns else to_ns(df['timestamp'])
    if len(stamps) < 2:
        return 0
    return int(np.median(np.diff(stamps)))


def align_index(df, timestamp, duration_ns=None):
    """
    Индекс последней ПОЛНОСТЬЮ ЗАКРЫТОЙ свечи df на момент `timestamp`.

    Нужен для связи таймфреймов: решение принимается на 1H, а bias читается
    с 1D. Здесь легко получить подглядывание в будущее, и оно не заметно
    глазом: дневная свеча текущего дня начинается в 00:00, её метка времени
    МЕНЬШЕ текущего момента — но её high/low/close агрегируют весь день,
    включая часы, которые ещё не наступили. Сравнение «метка <= сейчас»
    поэтому недостаточно: нужно, чтобы свеча успела ЗАКРЫТЬСЯ,
    то есть начало + длительность <= момента решения.

    duration_ns=None сохраняет старое поведение (сравнение по метке начала) —
    это допустимо только для того фрейма, на свече которого мы и стоим.

    Возвращает -1, если закрытых свечей на этот момент ещё нет.
    """
    stamps = df['_ts_ns'].to_numpy() if '_ts_ns' in df.columns else to_ns(df['timestamp'])

    target = pd.Timestamp(timestamp)
    target = target.tz_localize('UTC') if target.tzinfo is None else target.tz_convert('UTC')
    target_ns = target.as_unit('ns').value

    if duration_ns:
        stamps = stamps + duration_ns
    return int(np.searchsorted(stamps, target_ns, side='right')) - 1


class MarketContext:
    """
    Предрасчитанный контекст одной торговой пары на всех таймфреймах.

    frames — словарь {'bias': df, 'htf': df, 'poi': df, 'ltf': df}.
    Обязателен только 'poi'; остальные опциональны и просто отключают
    соответствующие факторы, если данных нет.
    """

    def __init__(self, frames, pair=None):
        if 'poi' not in frames or frames['poi'] is None:
            raise ValueError('MarketContext требует как минимум фрейм poi')

        self.pair = pair
        self.frames = {k: v for k, v in frames.items() if v is not None and len(v)}

        # Кэш наносекунд для быстрого align_index (см. to_ns о разрешении)
        for df in self.frames.values():
            if '_ts_ns' not in df.columns and 'timestamp' in df.columns:
                df['_ts_ns'] = to_ns(df['timestamp'])

        # Длительность свечи каждого фрейма — чтобы читать со старших ТФ
        # только полностью закрытые свечи (см. align_index)
        self._durations = {key: bar_duration_ns(df) for key, df in self.frames.items()}

        df_poi = self.frames['poi']

        # ── Тяжёлый предрасчёт, один раз на пару ─────────────────────────
        self.structure = structure_mod.build_structure(df_poi, tier='swing')
        self.minor_structure = structure_mod.build_structure(df_poi, tier='minor')
        self.pools = liquidity.find_liquidity_pools(df_poi, self.structure)
        self.sweeps = liquidity.find_sweeps(df_poi, self.pools)
        self.pois = poi_mod.collect_pois(df_poi, self.structure, self.sweeps)
        # Индекс по времени подтверждения: на каждой свече релевантны только
        # зоны моложе POI_MAX_AGE_BARS. Без этого evaluate перебирал бы все
        # тысячи зон на каждой из тысяч свечей.
        self._poi_confirmed = [p['confirmed_at'] for p in self.pois]
        self.fvgs = imbalance.find_fvg(df_poi)
        self.killzones = sessions.killzone_mask(df_poi)

        # Структуры старших ТФ — только для определения bias
        self.bias_structure = (
            structure_mod.build_structure(self.frames['bias'], tier='swing')
            if 'bias' in self.frames else None
        )
        self.htf_structure = (
            structure_mod.build_structure(self.frames['htf'], tier='swing')
            if 'htf' in self.frames else None
        )

    # ── Bias ──────────────────────────────────────────────────────────────
    def bias_at(self, timestamp):
        """
        Направление старшего порядка (§2.6).

        Согласуем два уровня: 1D задаёт общий bias, 4H уточняет. Если они
        противоречат друг другу — NEUTRAL, торговать нечего: это ровно та
        ситуация «ложного слома структуры на младшем ТФ» из §2.3.
        """
        # Момент решения — ЗАКРЫТИЕ текущей свечи рабочего ТФ, а не её начало:
        # timestamp хранит время открытия свечи (соглашение ccxt).
        decision_ts = pd.Timestamp(timestamp)
        decision_ts = (decision_ts.tz_localize('UTC') if decision_ts.tzinfo is None
                       else decision_ts.tz_convert('UTC'))
        decision_ts += pd.Timedelta(self._durations.get('poi', 0), unit='ns')

        trends = {}
        for key, struct in (('bias', self.bias_structure), ('htf', self.htf_structure)):
            if struct is None:
                continue
            # Только полностью закрытые свечи старшего ТФ
            idx = align_index(self.frames[key], decision_ts,
                              duration_ns=self._durations.get(key))
            if idx < 0:
                continue
            # Достаточно ли ИСТОРИИ НА ЭТОТ МОМЕНТ, а не в наборе данных
            # целиком. Проверять длину фрейма нельзя: бэктест строит контекст
            # один раз на всей истории, и длина там — свойство файла, а не
            # момента решения. Живой бот на той же свече видел бы меньше, и
            # два пути расходились бы по построению.
            if idx + 1 < params.MIN_HTF_BARS:
                continue
            if params.BIAS_REQUIRE_CONFIRMED:
                # §2.2: тренд подтверждён только последовательностью
                # HH-HL-HH. В боковике такой последовательности нет, состояние
                # остаётся NEUTRAL, и стратегия не торгует — это и есть
                # фильтр режима.
                trends[key] = structure_mod.confirmed_trend_at(struct, idx)
            else:
                trends[key] = structure_mod.state_at(struct, idx)['trend']

        if not trends:
            return NEUTRAL

        mode = params.BIAS_MODE

        # Режим обязан получить ИМЕННО те таймфреймы, на которых он определён.
        # Раньше отсутствие дневного фрейма (короткая история пары, обрезанная
        # выдача биржи) не мешало: голосов оставалось меньше, и `all()` по
        # одному голосу возвращал его же. Строгое «1D и 4H должны совпасть»
        # молча вырождалось в «только 4H» — по замерам худший режим (+26.1%
        # против +86.0%). Торговать при неизвестном bias — ровно то, что этот
        # фильтр и должен запрещать.
        required = {'agree': ('bias', 'htf'), 'bias_only': ('bias',),
                    'htf_only': ('htf',),
                    'htf_unless_against': ('bias', 'htf')}.get(mode, ())
        if any(key not in trends for key in required):
            return NEUTRAL
        if mode == 'bias_only':
            return trends.get('bias', NEUTRAL)
        if mode == 'htf_only':
            return trends.get('htf', NEUTRAL)
        if mode == 'htf_unless_against':
            # Направление задаёт 4H; дневная свеча имеет право только запретить.
            #
            # ВАЖНО: при BIAS_REQUIRE_CONFIRMED=False (как сейчас) этот режим
            # совпадает с 'agree' сделка в сделку — замер дал 396 и 436,
            # одинаковые проценты и просадку. Причина: NEUTRAL не встречается
            # ни разу (BTC 1D: BULLISH 52%, BEARISH 48%, NEUTRAL 0%), потому
            # что тренд берётся из последнего события структуры, а оно всегда
            # куда-то указывает. Состояния «1D в боковике» не существует, и
            # запрещать нечего сверх того, что уже запретило 'agree'.
            #
            # Смысл режим приобретает только вместе с BIAS_REQUIRE_CONFIRMED=
            # True, где NEUTRAL появляется. Сам этот флаг проверен и отвергнут
            # (80 сделок вместо 396, -16.1% на медведе), так что пара остаётся
            # на будущее, а не для текущей конфигурации.
            htf = trends['htf']
            bias = trends['bias']
            if htf == NEUTRAL or (bias != NEUTRAL and bias != htf):
                return NEUTRAL
            return htf
        if mode == 'any':
            # При противоречии верим старшему таймфрейму (§2.6: приоритет
            # всегда у более старшего ТФ)
            for key in ('bias', 'htf'):
                if trends.get(key, NEUTRAL) != NEUTRAL:
                    return trends[key]
            return NEUTRAL

        # 'agree' — строгое согласие обоих таймфреймов
        votes = list(trends.values())
        if all(v == BULLISH for v in votes):
            return BULLISH
        if all(v == BEARISH for v in votes):
            return BEARISH
        return NEUTRAL

    # ── Основная оценка ───────────────────────────────────────────────────
    def evaluate(self, at_index, balance=10_000.0):
        """
        Строит сетап на свече `at_index` рабочего ТФ или возвращает None.

        Второе значение — причина отказа (строкой), чтобы бот и бэктест
        могли логировать воронку отсева, а не молча пропускать пары.
        """
        df = self.frames['poi']
        if at_index < 30 or at_index >= len(df):
            return None, 'мало данных'

        timestamp = df['timestamp'].iloc[at_index]
        price = float(df['close'].iloc[at_index])

        # 0) Торговая сессия (§11.2) — как жёсткий фильтр, если включён
        if params.KILLZONE_AS_GATE and not self.killzones[at_index]:
            return None, 'вне killzone'

        # 1) Bias старшего ТФ
        bias = self.bias_at(timestamp)
        if bias == NEUTRAL:
            return None, 'bias старшего ТФ нейтрален'

        # 2) Последняя импульсная нога на рабочем ТФ — база для сетки Фибо
        leg = structure_mod.last_leg(self.structure, at_index)
        if leg is None:
            return None, 'нет импульсной ноги'
        if leg['direction'] != bias:
            return None, f'нога {leg["direction"]} против bias {bias}'

        # Длина ноги: разбор сделок показал, что ноги 10-20 свечей дают лучший
        # результат на обоих периодах, а очень короткие и очень длинные хуже.
        leg_bars = leg['end']['index'] - leg['start']['index']
        if params.LEG_BARS_MIN and leg_bars < params.LEG_BARS_MIN:
            return None, f'нога {leg_bars} свечей короче {params.LEG_BARS_MIN}'
        if params.LEG_BARS_MAX and leg_bars > params.LEG_BARS_MAX:
            return None, f'нога {leg_bars} свечей длиннее {params.LEG_BARS_MAX}'
        if fib.is_invalidated(price, leg):
            return None, 'сетап инвалидирован (цена за 88.6%)'

        # 3) Зоны интереса в направлении bias.
        # Берём только окно свежих зон — см. _poi_confirmed.
        lo = bisect_left(self._poi_confirmed, at_index - params.POI_MAX_AGE_BARS)
        hi = bisect_right(self._poi_confirmed, at_index)
        window = self.pois[lo:hi]
        if params.POI_TYPES_ENABLED:
            window = [p for p in window if p['type'] in params.POI_TYPES_ENABLED]

        candidates = poi_mod.active_pois(df, window, at_index, direction=bias)
        if not candidates:
            return None, 'нет активных POI'

        # 4) Фильтр premium/discount (§10.1)
        if params.REQUIRE_PREMIUM_DISCOUNT:
            valid = [p for p in candidates
                     if fib.is_valid_side(p['entry_near'], leg, bias)]
            if not valid:
                return None, 'POI вне дисконта/премиума'
            candidates = valid

        # 4.5) Зона OTE как жёсткое условие (§10.1). Самый сильный предиктор
        # из найденных: вход в OTE даёт +0.42R на медвежьем периоде против
        # -0.24R вне её. В качестве мягкого фактора этот сигнал терялся.
        if params.REQUIRE_OTE:
            in_ote = [p for p in candidates if fib.in_ote(p['entry_near'], leg)]
            if not in_ote:
                return None, 'ни одна зона не попадает в OTE'
            candidates = in_ote

        # 5) Цена ещё не дошла до зоны — лимит должен «отдыхать» впереди хода
        pending = [p for p in candidates if self._is_ahead(p, price, bias)]
        if not pending:
            return None, 'цена уже прошла зону'
        candidates = pending

        # 6) Выбираем лучшую зону по совокупности факторов
        swept = liquidity.recent_sweep(self.sweeps, at_index, direction=bias)
        active_gaps = imbalance.active_fvgs(df, self.fvgs, at_index, direction=bias)

        scored = []
        for candidate in candidates:
            gap = imbalance.nearest_fvg(active_gaps, candidate['entry_near'], direction=bias)
            extras = {'fvg_present': gap is not None, 'liquidity_swept': swept is not None}
            scored.append((poi_mod.score_poi(candidate, extras), candidate, gap))
        scored.sort(key=lambda x: -x[0])
        poi_score, best, best_gap = scored[0]

        # 7) Confluence по чек-листу §23
        factors, score = self._confluence(
            df, at_index, best, leg, bias, swept, best_gap, timestamp
        )
        # Порог у покупок может быть выше: замер на двух независимых периодах и
        # во всех трёх режимах рынка показал, что лонги слабее шортов ВЕЗДЕ
        # (0.144 R против 0.390 R при 46% и 54% сделок). Премия равна нулю —
        # поведение прежнее, симметричное.
        threshold = params.MIN_CONFLUENCE_SCORE
        if bias == BULLISH:
            threshold += params.LONG_CONFLUENCE_PREMIUM
        if score < threshold:
            missing = [k for k, v in factors.items() if not v]
            return None, f'confluence {score:.1f} < {threshold} (нет: {", ".join(missing)})'

        # 8) Геометрия сделки
        trade = self._build_trade(best, leg, bias, swept, at_index, balance)
        if trade is None:
            return None, 'геометрия не собралась'
        if trade['rr'] < params.MIN_RR:
            return None, f'RR {trade["rr"]:.2f} < {params.MIN_RR}'
        # Слишком далёкая цель — нереалистичный сценарий, а не хорошая сделка:
        # сетапы с RR выше 12 убыточны на обоих рыночных режимах.
        if params.MAX_RR and trade['rr'] > params.MAX_RR:
            return None, f'RR {trade["rr"]:.2f} > {params.MAX_RR}'

        return {
            'pair': self.pair,
            'index': at_index,
            'time': timestamp,
            'direction': bias,
            'poi': best,
            'poi_score': round(poi_score, 3),
            'leg': leg,
            'sweep': swept,
            'fvg': best_gap,
            'factors': factors,
            'confluence': round(score, 2),
            'params': trade,
        }, None

    # ── Внутренние помощники ──────────────────────────────────────────────
    @staticmethod
    def _is_ahead(candidate, price, direction):
        """
        Зона ещё впереди по ходу коррекции: для лонга цена ВЫШЕ зоны и
        должна опуститься в неё, для шорта — наоборот. Если цена уже внутри
        или за зоной, лимитный вход бессмыслен.
        """
        if direction == BULLISH:
            return price > candidate['entry_near']
        return price < candidate['entry_near']

    def _confluence(self, df, at_index, candidate, leg, bias, swept, gap, timestamp):
        """Считает факторы подтверждения и их суммарный вес (§23)."""
        weights = params.CONFLUENCE_WEIGHTS
        entry = candidate['entry_near']

        correction_bars = at_index - leg['end']['index']
        recent_break = structure_mod.state_at(self.structure, at_index)['last_event']

        factors = {
            'htf_bias_aligned': True,   # без этого мы бы уже вышли выше
            'liquidity_swept': swept is not None,
            'premium_discount': fib.is_valid_side(entry, leg, bias),
            'poi_fresh': candidate.get('touches', 0) == 0,
            'fvg_present': gap is not None,
            'structure_break': recent_break is not None and recent_break['direction'] == bias,
            'ote_zone': fib.in_ote(entry, leg),
            'killzone': bool(self.killzones[at_index]),
            'law_of_effort': fib.law_of_effort(leg, correction_bars),
        }
        score = sum(weights.get(name, 0.0) for name, ok in factors.items() if ok)
        return factors, score

    def _build_trade(self, candidate, leg, direction, swept, at_index, balance):
        """
        Вход, стоп, цели, размер позиции.

        Стоп (§5.2, §5.3):
            aggressive   — за дальнюю границу зоны: выше RR, ниже винрейт;
            conservative — за экстремум снятия ликвидности: ниже RR, выше
                           винрейт (при отсутствии свипа падает до границы зоны).

        Цели: расширения сетки −0.27/−0.62/−1.0 (§10.2). Если по пути стоит
        непротестированный пул ликвидности, он становится первой целью —
        §14.2 требует ставить тейки на очевидных пулах.
        """
        depth = params.POI_ENTRY_DEPTH
        entry = (candidate['entry_near'] * (1 - depth) + candidate['entry_mid'] * depth)

        # Отступ наружу от зоны: лимит встаёт навстречу цене и наливается чаще.
        # Половина сетапов иначе теряется — цена разворачивается, не дойдя до
        # границы. Платим за это чуть худшей ценой входа и, соответственно,
        # чуть большим стопом.
        if params.POI_ENTRY_OFFSET:
            span = abs(candidate['top'] - candidate['bottom'])
            shift = span * params.POI_ENTRY_OFFSET
            entry = entry + shift if direction == BULLISH else entry - shift

        far_edge = candidate['invalidation']
        if params.SL_MODE == 'conservative' and swept is not None:
            extreme = swept['extreme']
            far_edge = min(far_edge, extreme) if direction == BULLISH else max(far_edge, extreme)

        buffer_ = params.SL_BUFFER_PCT
        if direction == BULLISH:
            stop = far_edge * (1 - buffer_)
            min_stop = entry * (1 - params.MIN_SL_PCT)
            stop = min(stop, min_stop)
        else:
            stop = far_edge * (1 + buffer_)
            min_stop = entry * (1 + params.MIN_SL_PCT)
            stop = max(stop, min_stop)

        sl_distance = abs(entry - stop)
        if sl_distance <= 0:
            return None

        raw_targets = fib.targets(leg, entry=entry)
        if not raw_targets:
            return None

        # Гибридный режим: первая цель на фиксированном кратном риска.
        # Расширения сетки отсчитываются за конец импульса, а вход стоит
        # глубоко в коррекции — цене нужно пройти всю ногу обратно, чтобы дать
        # хотя бы первый тейк. Близкая цель ловит движения, которые иначе
        # заканчиваются чистым стопом.
        if params.TP_MODE == 'hybrid':
            near = (entry + params.TP1_R_MULTIPLE * sl_distance if direction == BULLISH
                    else entry - params.TP1_R_MULTIPLE * sl_distance)
            farther = [
                t for t in raw_targets
                if (t > near if direction == BULLISH else t < near)
            ]
            raw_targets = [near] + farther

        if params.TP_MODE == 'liquidity':
            raw_targets = self._liquidity_targets(
                direction, entry, sl_distance, at_index,
                count=len(params.TP_CLOSE_FRACTIONS), fallback=raw_targets)
            if not raw_targets:
                return None
        else:
            # Непротестированный пул ликвидности ближе первой цели — берём его
            pools = liquidity.untapped_pools(
                self.pools, self.sweeps, at_index,
                side=liquidity.BSL if direction == BULLISH else liquidity.SSL,
            )
            ahead = [
                p['price'] for p in pools
                if (p['price'] > entry if direction == BULLISH else p['price'] < entry)
            ]
            if ahead:
                nearest = min(ahead) if direction == BULLISH else max(ahead)
                first = raw_targets[0]
                closer = nearest < first if direction == BULLISH else nearest > first
                if closer and abs(nearest - entry) / sl_distance >= 1.0:
                    raw_targets = [nearest] + raw_targets

        fractions = list(params.TP_CLOSE_FRACTIONS)
        targets = raw_targets[:len(fractions)]
        fractions = fractions[:len(targets)]
        # Остаток веса вешаем на последнюю цель, чтобы сумма долей была ровно 1
        if fractions:
            fractions[-1] += 1.0 - sum(fractions)

        # RR считаем взвешенно по долям частичной фиксации (§16): это честная
        # метрика сделки, а не RR до самого дальнего тейка, до которого доходит
        # лишь часть позиции.
        weighted_gain = sum(
            frac * abs(target - entry) for frac, target in zip(fractions, targets)
        )
        rr = weighted_gain / sl_distance

        risk_amount = balance * (params.RISK_PER_TRADE_PCT / 100)
        position_size = risk_amount / sl_distance

        return {
            'entry': float(entry),
            'stop_loss': float(stop),
            'targets': [float(t) for t in targets],
            'fractions': [float(f) for f in fractions],
            'rr': float(rr),
            'rr_first': float(abs(targets[0] - entry) / sl_distance),
            'rr_final': float(abs(targets[-1] - entry) / sl_distance),
            'sl_distance': float(sl_distance),
            'position_size': float(position_size),
            'risk_amount': float(risk_amount),
            'sl_mode': params.SL_MODE,
        }

    def _liquidity_targets(self, direction, entry, sl_distance, at_index,
                           count, fallback):
        """
        Цели на непротестированных пулах ликвидности (§14.2).

        Смысл прямой: цена ходит к ликвидности, а не к уровням сетки. Пулы —
        это скопления стопов за очевидными хаями и лоями (свинги, равные
        вершины, экстремумы дня/недели/месяца), и именно туда рынок стремится.
        Расширения Фибоначчи такого обоснования не имеют: они отсчитываются за
        конец импульса и часто оказываются там, где ликвидности нет вовсе, —
        отсюда 68% сделок, не дошедших ни до одной цели при среднем ходе 2.7R.

        Пулы берутся только непротестированные: снятый уровень ликвидности
        больше не притягивает цену. Сортировка по расстоянию, ближние первыми.
        """
        pools = liquidity.untapped_pools(
            self.pools, self.sweeps, at_index,
            side=liquidity.BSL if direction == BULLISH else liquidity.SSL,
        )
        bullish = direction == BULLISH

        ahead = [
            p for p in pools
            if (p['price'] > entry if bullish else p['price'] < entry)
            and p.get('weight', 0) >= params.LIQ_MIN_WEIGHT
            # Цель ближе минимума не окупает комиссию и проскальзывание
            and abs(p['price'] - entry) / sl_distance >= params.LIQ_MIN_R
        ]
        ahead.sort(key=lambda p: abs(p['price'] - entry))

        # Уровни часто дублируются: свинговый хай и хай прошлого дня могут
        # стоять в одной точке. Две цели по одной цене раздробили бы позицию
        # без всякого смысла, поэтому близкие пулы сливаем, оставляя значимый.
        merged = []
        for pool in ahead:
            if merged and abs(pool['price'] - merged[-1]['price']) / entry < params.LIQ_MERGE_PCT:
                if pool.get('weight', 0) > merged[-1].get('weight', 0):
                    merged[-1] = pool
                continue
            merged.append(pool)

        targets = [float(p['price']) for p in merged[:count]]
        if not targets:
            # Впереди нет ни одного непройденного пула — работаем по сетке,
            # иначе потеряли бы сетап целиком там, где он может быть хорош.
            return fallback

        # Пулов меньше, чем долей фиксации: остаток добираем расширениями
        # сетки, лежащими ДАЛЬШЕ последнего пула.
        if len(targets) < count:
            last = targets[-1]
            extra = [t for t in fallback
                     if (t > last if bullish else t < last)]
            targets += extra[:count - len(targets)]
        return targets


def build_context(frames, pair=None):
    """Фабрика контекста — точка входа для бота и бэктеста."""
    return MarketContext(frames, pair=pair)
