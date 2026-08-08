"""
Адаптер SMC-ядра под интерфейс живого бота.

Задача модуля — отдать trade_manager сигнал ровно в том формате, который он
уже умеет исполнять (как это делает strategy.analyze_market для фибо-версии),
не таща знание о бирже и ордерах внутрь чистого пакета smc/.

Две вещи, которые здесь решаются и которых нет в бэктесте:

1. НЕЗАКРЫТАЯ СВЕЧА. ccxt отдаёт последней ещё формирующуюся свечу. Считать
   по ней структуру нельзя: её high/low меняются каждую секунду, свинги и
   зоны будут «прыгать». Отбрасываем её и работаем по закрытым.

2. КЭШ КОНТЕКСТА. Построение структуры/зон/свипов — тяжёлая операция, а бот
   сканирует пул каждые 5 минут при часовом рабочем ТФ. Пересчитываем
   контекст только когда появилась новая закрытая часовая свеча.
"""

import config
import scan_report as report
import settings_store as settings
from exchange import fetch_ohlcv
from logger import log
from smc import params as smc_params
# Псевдоним обязателен: ниже определена функция market_regime(), и без
# него она перекрыла бы модуль. Ошибка была бы молчаливой — вызов
# обёрнут в try, и режим просто перестал бы определяться.
import market_regime as regime_state
from smc import regime as regime_mod
from smc import signal as smc_signal

# Причина отказа по последней проверенной паре. Ядро возвращает её вторым
# значением, а адаптер до сих пор только писал в лог и терял — теперь она
# нужна дашборду, чтобы показать воронку отсева.
_last_reason = {}


def _apply_settings():
    """
    Переносит настройки оператора в параметры ядра.

    Ядро `smc/` намеренно не знает ни про config, ни про настройки — оно
    считает по числам в своём модуле. Поэтому значения, меняемые из дашборда,
    проставляются здесь, в адаптере, перед каждым разбором пары.
    """
    smc_params.RISK_PER_TRADE_PCT = settings.risk_pct('SMC')
    smc_params.MIN_SL_PCT = settings.min_stop_pct('SMC')

# pair -> (последний timestamp закрытой свечи, контекст)
_context_cache = {}

# (дата последней закрытой дневной свечи) -> (режим, er, порог, множитель).
# Режим меняется раз в сутки, тянуть дневные свечи BTC на каждой паре
# бессмысленно.
_regime_cache = {}


def regime_snapshot():
    """
    Последний посчитанный режим БЕЗ обращения к бирже.

    Отдельная функция нужна дашборду. Он обновляется раз в несколько секунд,
    и если бы он звал market_regime(), каждый его запрос тянул бы дневные
    свечи с биржи: при недоступной сети страница висла бы на таймауте
    HTTP-запроса, а отображение состояния не должно зависеть от того,
    отвечает ли биржа.
    """
    if not _regime_cache:
        return None, 1.0, 'режим ещё не считался'
    name, er, threshold, mult = next(iter(_regime_cache.values()))
    return name, mult, regime_mod.describe(name, er, threshold, mult)


def market_regime(client=None):
    """
    Режим рынка по дневным свечам BTC и множитель риска к нему.

    Возвращает (режим, множитель, описание). При любой неудаче — полный
    размер: правило умеет только уменьшать ставку, и отсутствие данных не
    повод торговать иначе, чем обычно.
    """
    need = regime_state.ER_WINDOW + regime_state.MIN_HISTORY + 30
    try:
        raw = fetch_ohlcv('1d', limit=need, symbol=regime_state.SYMBOL,
                          client=client)
        df = _drop_forming_candle(raw)
        if df is None or len(df) < regime_state.ER_WINDOW + 2:
            return regime_mod.UNKNOWN, 1.0, 'режим неизвестен (нет дневных данных)'

        key = str(df['timestamp'].iloc[-1])
        if key in _regime_cache:
            name, er, threshold, mult = _regime_cache[key]
            return name, mult, regime_mod.describe(name, er, threshold, mult)

        name, er, threshold = regime_mod.classify(df['close'].to_numpy())
        mult = regime_mod.risk_multiplier(name)
        _regime_cache.clear()
        _regime_cache[key] = (name, er, threshold, mult)
        return name, mult, regime_mod.describe(name, er, threshold, mult)
    except Exception as exc:
        log(f"   режим рынка не определён ({exc}) — риск полный")
        return regime_mod.UNKNOWN, 1.0, 'режим неизвестен (ошибка)'


def _drop_forming_candle(df):
    """Убирает последнюю (ещё не закрытую) свечу."""
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def _load_frames(pair, client=None):
    """
    Грузит все нужные таймфреймы для пары.

    Раскладка по решению пользователя: 1D задаёт bias, 4H уточняет структуру
    старшего порядка, 1H — рабочий ТФ поиска зон.
    """
    frames = {}
    for key, timeframe, limit in (
        ('bias', smc_params.TF_BIAS, smc_params.LOOKBACK_BIAS),
        ('htf', smc_params.TF_HTF, smc_params.LOOKBACK_HTF),
        ('poi', smc_params.TF_POI, smc_params.LOOKBACK_POI),
    ):
        raw = fetch_ohlcv(timeframe, limit=limit + 5, symbol=pair, client=client)
        closed = _drop_forming_candle(raw)
        # Достаточность истории проверяет ЯДРО на момент решения
        # (params.MIN_HTF_BARS в bias_at). Здесь только отсутствие данных:
        # длина фрейма — не то же самое, что число закрытых свечей на свече
        # решения, и дублировать правило по длине значит раздвоить его.
        if closed is None or len(closed) < 2:
            if key == 'poi':
                return None
            # Пропуск старшего ТФ не молчаливый: без него режим определения
            # направления не может работать как задуман, и ядро вернёт
            # NEUTRAL. Знать об этом надо — иначе пара просто «не торгуется»
            # без объяснимой причины.
            log(f"   {pair}: нет данных {timeframe} "
                f"({len(closed) if closed is not None else 0} свечей) — "
                f"направление старшего порядка определить нечем")
            continue
        frames[key] = closed
    return frames if 'poi' in frames else None


def get_context(pair, client=None, force=False):
    """
    Контекст пары, пересобираемый только при появлении новой закрытой свечи
    рабочего ТФ.
    """
    frames = _load_frames(pair, client=client)
    if frames is None:
        return None

    last_ts = frames['poi']['timestamp'].iloc[-1]
    cached = _context_cache.get(pair)
    if cached and not force and cached[0] == last_ts:
        return cached[1]

    context = smc_signal.build_context(frames, pair=pair)
    _context_cache[pair] = (last_ts, context)
    return context


def _to_bot_signal(setup, pair, balance, risk_scale=1.0):
    """
    Переводит сетап SMC в структуру, понятную trade_manager.

    Поля take_profit_1/2 остаются для журнала, графиков и Telegram, но план
    выхода исполнитель берёт из tp_targets/tp_fractions — там ВСЕ цели, включая
    третью. Частичная фиксация для SMC принципиальна: по бэктесту конфигурация
    с одним тейком уходит в минус (−9.1% против +40.6% на трёх), потому что
    основную прибыль дают дальние цели при винрейте около 25%.
    """
    trade = setup['params']
    direction = 'LONG' if setup['direction'] == 'BULLISH' else 'SHORT'
    leg = setup['leg']
    targets = trade['targets']

    params = {
        'entry': trade['entry'],
        'stop_loss': trade['stop_loss'],
        'take_profit_1': targets[0],
        'take_profit_2': targets[1] if len(targets) > 1 else targets[0],
        # Безубыток по §14.1 ВЫКЛЮЧЕН по результатам бэктеста: подтянутый стоп
        # выбивает позицию шумом коррекции до дальних целей, а именно они и
        # дают прибыль (+88.5% против +54.2% при включённом безубытке).
        'be_level': targets[0] if smc_params.BREAKEVEN_AFTER_TP1 else None,
        'breakeven_after_tp': bool(smc_params.BREAKEVEN_AFTER_TP1),
        'tp_targets': list(targets),
        'tp_fractions': list(trade['fractions']),
        'max_same_direction': smc_params.MAX_SAME_DIRECTION,
        # Риск режется режимом рынка ЗДЕСЬ, в одном месте: и paper_broker, и
        # trade_manager считают объём от params['risk_pct'], поэтому фантом и
        # бой получают одинаковый размер по построению, а не по совпадению.
        'risk_pct': settings.risk_pct('SMC') * risk_scale,
        'risk_scale': risk_scale,
        'position_size': trade['position_size'] * risk_scale,
        'risk_amount': trade['risk_amount'] * risk_scale,
        'rr': trade['rr'],
        'sl_distance': trade['sl_distance'],
    }

    poi = setup['poi']
    return {
        'trading_pair': pair,
        'setup': {
            'type': direction,
            'start_price': leg['start']['price'],
            'end_price': leg['end']['price'],
            'size': leg['size'],
            'start_time': leg['start']['time'],
            'end_time': leg['end']['time'],
        },
        'trigger': {
            'zone': poi['type'],
            'entry_type': smc_params.ENTRY_MODE,
            'trigger_price': trade['entry'],
        },
        'params': params,
        'htf_trend': setup['direction'],
        # Богатый контекст для журнала, графика и Telegram
        'smc': {
            'poi_type': poi['type'],
            'poi_top': poi['top'],
            'poi_bottom': poi['bottom'],
            'confluence': setup['confluence'],
            'factors': setup['factors'],
            'targets': targets,
            'fractions': trade['fractions'],
            'rr_first': trade['rr_first'],
            'rr_final': trade['rr_final'],
            'sweep': (setup['sweep'] or {}).get('source'),
            'sl_mode': trade['sl_mode'],
            # ИМБАЛАНС ДОВОДИТСЯ ДО СИГНАЛА. Он участвует в отборе — входит в
            # confluence, — но до графика не доходил, и на картинке было видно
            # только ордер-блок. Разобрать сделку по такому графику нельзя:
            # половина основания решения оставалась за кадром.
            'fvg_top': (setup.get('fvg') or {}).get('top'),
            'fvg_bottom': (setup.get('fvg') or {}).get('bottom'),
            # ОТ ЧЕГО СТРОИЛАСЬ СТРУКТУРА. Ордер-блок отвечает на вопрос «где
            # вход», но не на вопрос «почему вообще лонг». Ответ на второй —
            # пробитый структурный уровень и снятая перед ним ликвидность; оба
            # значатся в факторах отбора (structure_break, liquidity_swept), но
            # до графика не доходили ни числом, ни линией. По такой картинке
            # разобрать сделку нельзя: видно следствие и не видно причины.
            'structure_type': (setup.get('structure') or {}).get('type'),
            'structure_level': (setup.get('structure') or {}).get('level'),
            'sweep_side': (setup.get('sweep') or {}).get('side'),
            # ЦЕНА СНЯТИЯ ЛЕЖИТ В `level`, А НЕ В `price`. Поле `price` у
            # снятия тоже есть, но внутри вложенного `pool`, и обращение к
            # верхнему уровню молча отдавало None: сторона подписывалась, линия
            # не рисовалась. Проверено на настоящих сетапах — на выдуманном
            # словаре в тесте ошибка не проявлялась.
            'sweep_price': (setup.get('sweep') or {}).get('level'),
            # Насколько глубоко вынесли за уровень. Само снятие — это уровень,
            # но экстремум показывает длину тени, которой его забрали.
            'sweep_extreme': (setup.get('sweep') or {}).get('extreme'),
        },
    }


def analyze_market(pair, balance, client=None, risk_scale=None):
    """
    Проверяет одну пару и возвращает сигнал либо None.

    Сигнатура намеренно отличается от strategy.analyze_market(df_1h, df_5m,
    pair, balance): SMC сам решает, какие таймфреймы ему нужны, и тянет их
    самостоятельно — передавать готовые окна снаружи здесь бессмысленно.
    """
    _apply_settings()
    if risk_scale is None:
        _, risk_scale, _ = market_regime(client=client)
    context = get_context(pair, client=client)
    if context is None:
        log(f"   {pair}: недостаточно данных для SMC-контекста")
        _last_reason[pair] = 'мало данных по паре'
        return None

    last_index = len(context.frames['poi']) - 1
    setup, reason = context.evaluate(last_index, balance=balance)
    _last_reason[pair] = reason

    if setup is None:
        log(f"   {pair}: нет сигнала — {reason}")
        return None

    log(f"   {pair}: {setup['direction']} {setup['poi']['type']} | "
        f"confluence {setup['confluence']} | RR {setup['params']['rr']:.2f}")
    return _to_bot_signal(setup, pair, balance, risk_scale=risk_scale)


def scan_for_setups(pairs, trade_manager, client=None, balance=None):
    """
    Аналог pair_scanner.scan_for_setups на SMC-ядре.

    Возвращает список кандидатов, отсортированный по confluence (лучшие
    первыми) — именно этот порядок определяет, кого пробовать при нехватке
    свободных слотов.
    """
    balance = config.BALANCE if balance is None else balance
    candidates = []
    report.begin('SMC')

    # Режим считается один раз на цикл, а не на каждой паре: он общий для
    # всего рынка и меняется раз в сутки.
    _, risk_scale, regime_text = market_regime(client=client)
    log(f"   рынок: {regime_text}")

    for pair in pairs:
        try:
            if not trade_manager.check_cooldown(pair):
                log(f"   {pair}: кулдаун активен, пропускаем")
                report.record('SMC', pair, 'кулдаун активен')
                continue
            if trade_manager.has_position_or_order(pair):
                log(f"   {pair}: уже есть позиция или ордер, пропускаем")
                report.record('SMC', pair, 'позиция или ордер уже есть')
                continue

            signal = analyze_market(pair, balance, client=client,
                                    risk_scale=risk_scale)
            report.record('SMC', pair, None if signal else _last_reason.get(pair))
            if signal:
                context = _context_cache.get(pair)
                candidates.append({
                    'pair': pair,
                    'signal': signal,
                    'score': signal['smc']['confluence'],
                    'rr': signal['params']['rr'],
                    'poi_type': signal['smc']['poi_type'],
                    # Свечи рабочего ТФ — для графика сделки в Telegram.
                    # Контекст уже загрузил их, повторный запрос к бирже не нужен.
                    'df_1h': context[1].frames['poi'] if context else None,
                })
        except Exception as exc:
            log(f"   {pair}: ошибка SMC-сканирования — {exc}")
            report.record('SMC', pair, f'ошибка сканирования: {exc}')

    report.finish('SMC')
    candidates.sort(key=lambda c: (-c['score'], -c['rr']))
    return candidates
