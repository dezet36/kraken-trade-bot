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

Обе вещи с 21.09.2026 живут в market_structure.py — общем слое структуры, который
читают и ИИ, и панель. Здесь адаптер только ПРИНИМАЕТ РЕШЕНИЯ SMC по этому
контексту и переводит их в сигнал брокера.
"""

from strategies import scan_report as report
from infra.logger import log
from strategies import settings
from strategies.smc import params as smc_params
# Псевдоним обязателен: ниже определена функция market_regime(), и без
# него она перекрыла бы модуль. Ошибка была бы молчаливой — вызов
# обёрнут в try, и режим просто перестал бы определяться.
from analysis import market_regime as regime_state
from strategies.smc import regime as regime_mod
from strategies.smc import signal as smc_signal

# Причина отказа по последней проверенной паре. Ядро возвращает её вторым
# значением, а адаптер до сих пор только писал в лог и терял — теперь она
# нужна дашборду, чтобы показать воронку отсева.
_last_reason = {}


# Что оператор вправе менять из панели: только РЕШЕНИЯ SMC (часть II
# smc/params). Структура рынка (часть I) — общий слой, её из панели не
# трогают: правка там изменила бы разметку для ИИ и другие стратегии. Риска
# здесь нет с 08.10.2026: деньги решает счёт стратегии (accounts/paper.py).
_OPERATOR_SETTINGS = {
    'MIN_SL_PCT': lambda: settings.min_stop_pct('SMC'),
}


def _apply_settings():
    """
    Переносит настройки оператора в параметры РЕШЕНИЙ ядра.

    Ядро `smc/` намеренно не знает ни про config, ни про настройки — оно
    считает по числам в своём модуле. Поэтому значения, меняемые из дашборда,
    проставляются здесь, в адаптере, перед каждым разбором пары. Писать можно
    только в имена из smc_params.DECISION — проверка стоит здесь, а не в
    тесте, чтобы новая настройка не могла молча уйти в общий слой.
    """
    for name, read in _OPERATOR_SETTINGS.items():
        if name not in smc_params.DECISION:
            raise RuntimeError(f'{name}: настройка оператора лезет в структуру рынка (smc/params, часть I)')
        setattr(smc_params, name, read())

# (дата последней закрытой дневной свечи) -> (режим, er, порог).
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
        return None, 'режим ещё не считался'
    name, er, threshold = next(iter(_regime_cache.values()))
    return name, regime_mod.describe(name, er, threshold)


def market_regime(client=None):
    """
    Режим рынка по дневным свечам BTC — для журнала. Возвращает (режим,
    описание). На риск не влияет с 08.10.2026: риск у всех стратегий один, его
    решает счёт (решение владельца; до этого в тренде SMC урезала размер вдвое).
    """
    need = regime_state.ER_WINDOW + regime_state.MIN_HISTORY + 30
    try:
        raw = fetch_ohlcv('1d', limit=need, symbol=regime_state.SYMBOL,
                          client=client)
        df = _drop_forming_candle(raw)
        if df is None or len(df) < regime_state.ER_WINDOW + 2:
            return regime_mod.UNKNOWN, 'режим неизвестен (нет дневных данных)'

        key = str(df['timestamp'].iloc[-1])
        if key in _regime_cache:
            name, er, threshold = _regime_cache[key]
            return name, regime_mod.describe(name, er, threshold)

        name, er, threshold = regime_mod.classify(df['close'].to_numpy())
        _regime_cache.clear()
        _regime_cache[key] = (name, er, threshold)
        return name, regime_mod.describe(name, er, threshold)
    except Exception as exc:
        log(f"   режим рынка не определён ({exc})")
        return regime_mod.UNKNOWN, 'режим неизвестен (ошибка)'


# ── Структурный контекст — ОБЩИЙ СЛОЙ (market_structure.py) ───────────────
# Свечи → закрытые свечи → MarketContext строит market_structure: его читают и
# SMC, и ИИ, и панель. Здесь остались псевдонимы для старого кода и тестов;
# правка адаптера SMC не может изменить то, что видит модель.
import market_structure as _context
# Данные — через дверь анализа (analysis/market.py), не со сборщиков и не с
# биржи напрямую (реорганизация, этап 4).
from analysis.market import fetch_ohlcv

_drop_forming_candle = _context.drop_forming_candle
_load_frames = _context.load_frames
cached_context = _context.cached
get_context = _context.get


def _to_bot_signal(setup, pair):
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


def _funding_rate(pair, client=None):
    """
    Последняя выплаченная ставка фандинга пары (доля за 8 ч) или None.

    Через positioning.settled_funding: выплата совпадает с закрытием часа, по
    которому SMC решает, а сборщик подхватывает её с опозданием до часа — фильтр
    же проверялся со ставкой, известной в момент выплаты (01.10.2026).
    """
    try:
        from analysis import market
        rate = market.settled_funding(pair, client=client)
        return None if rate is None else float(rate)
    except Exception as exc:                                  # noqa: BLE001
        log(f"   {pair}: фандинг не прочитан ({exc})")
        return None


def crowd_reason(direction, rate):
    """
    Причина отказа «толпа за сделку» или None (smc/params.FUNDING_AGAINST_CROWD).

    Толпа стоит в сторону сделки, если платит за неё фандинг: у лонга ставка
    выше порога (лонги платят шортам), у шорта — ниже минус порога. Такие
    сетапы SMC не берёт: на пяти периодах истории они в минусе, а против толпы —
    в плюсе во всех пяти (docs/SMC_исследование_2026-09.md). Нет ставки — нет и
    отказа: торговля от источника не зависит (CLAUDE.md, «Данные»).
    """
    if not smc_params.FUNDING_AGAINST_CROWD or rate is None:
        return None
    side = 1.0 if direction in ('BULLISH', 'LONG') else -1.0
    signed_bp = rate * 1e4 * side
    if signed_bp <= smc_params.FUNDING_MAX_BP:
        return None
    crowd = 'в лонгах' if rate > 0 else 'в шортах'
    return f'толпа за сделку (фандинг {rate * 1e4:+.2f} б.п., толпа {crowd})'


def _shadow_crowd_refusal(setup, pair, reason):
    """
    Отказ «толпа за сделку» — в тени (shadow.py): чем кончился бы сетап.

    Своих сделок у SMC с фильтром толпы ~45 в год, и проверять фильтр только по
    ним — год ожидания. Отказов больше, чем сделок, и исход каждого пишется в
    shadow_trades.csv: живое сравнение «против толпы» и «за толпу» копится на
    всех сетапах (docs/SMC_исследование_2026-09.md). Тень — не деньги: ни
    депозит, ни пределы, ни кулдаун бота она не трогает; её ошибка торговле не
    мешает.
    """
    try:
        # Тень пишет исполнение; стратегия шлёт в порт (infra/outbox).
        from infra.outbox import shadow
        signal = _to_bot_signal(setup, pair)
        shadow.watch('SMC', signal, 'толпа за сделку', reason)
    except Exception as exc:                                  # noqa: BLE001
        log(f"   {pair}: тень отказа по толпе не заведена ({exc})")


def analyze_market(pair, client=None):
    """
    Проверяет одну пару и возвращает сигнал либо None.

    Сигнатура намеренно отличается от strategy.analyze_market(df_1h, df_5m,
    pair): SMC сам решает, какие таймфреймы ему нужны, и тянет их
    самостоятельно — передавать готовые окна снаружи здесь бессмысленно.
    """
    _apply_settings()
    context = get_context(pair, client=client)
    if context is None:
        log(f"   {pair}: недостаточно данных для SMC-контекста")
        _last_reason[pair] = 'мало данных по паре'
        return None

    last_index = len(context.frames['poi']) - 1
    setup, reason = context.evaluate(last_index)
    _last_reason[pair] = reason

    if setup is None:
        log(f"   {pair}: нет сигнала — {reason}")
        return None

    # Против толпы — решение SMC, а не ядра: ядро не знает про биржу, а
    # фандинг лежит в общем слое (positioning).
    rate = _funding_rate(pair, client=client) if smc_params.FUNDING_AGAINST_CROWD else None
    if smc_params.FUNDING_AGAINST_CROWD and rate is None:
        log(f"   {pair}: фандинг неизвестен — фильтр толпы пропущен")
    blocked = crowd_reason(setup['direction'], rate)
    if blocked:
        _last_reason[pair] = blocked
        log(f"   {pair}: нет сигнала — {blocked}")
        _shadow_crowd_refusal(setup, pair, blocked)
        return None

    log(f"   {pair}: {setup['direction']} {setup['poi']['type']} | "
        f"confluence {setup['confluence']} | RR {setup['params']['rr']:.2f}"
        + (f" | фандинг {rate * 1e4:+.2f} б.п." if rate is not None else ''))
    signal = _to_bot_signal(setup, pair)
    # Ставка в момент решения — в журнал: по ней проверяется фильтр вживую.
    signal['smc']['funding_bp'] = None if rate is None else round(rate * 1e4, 3)
    return signal


def scan_for_setups(pairs, trade_manager, client=None):
    """
    Аналог pair_scanner.scan_for_setups на SMC-ядре.

    Возвращает список кандидатов, отсортированный по confluence (лучшие
    первыми) — именно этот порядок определяет, кого пробовать при нехватке
    свободных слотов.
    """
    candidates = []
    report.begin('SMC')

    # Режим считается один раз на цикл, а не на каждой паре: он общий для
    # всего рынка и меняется раз в сутки. Только для журнала: на риск не влияет.
    _, regime_text = market_regime(client=client)
    log(f"   рынок: {regime_text}")

    pool = smc_params.TRADE_POOL
    for pair in pairs:
        if pool and pair not in pool:
            # Свой пул SMC (smc/params.TRADE_POOL): вне его стратегия на
            # истории теряла. В лог не пишем — это каждая пара каждый цикл.
            report.record('SMC', pair, 'вне пула SMC')
            continue
        try:
            if not trade_manager.check_cooldown(pair):
                log(f"   {pair}: кулдаун активен, пропускаем")
                report.record('SMC', pair, 'кулдаун активен')
                continue
            if trade_manager.has_position_or_order(pair):
                log(f"   {pair}: уже есть позиция или ордер, пропускаем")
                report.record('SMC', pair, 'позиция или ордер уже есть')
                continue

            signal = analyze_market(pair, client=client)
            report.record('SMC', pair, None if signal else _last_reason.get(pair))
            if signal:
                context = _context.cached(pair)
                candidates.append({
                    'pair': pair,
                    'signal': signal,
                    'score': signal['smc']['confluence'],
                    'rr': signal['params']['rr'],
                    'poi_type': signal['smc']['poi_type'],
                    # Свечи рабочего ТФ — для графика сделки в Telegram.
                    # Контекст уже загрузил их, повторный запрос к бирже не нужен.
                    'df_1h': context.frames['poi'] if context else None,
                })
        except Exception as exc:
            log(f"   {pair}: ошибка SMC-сканирования — {exc}")
            report.record('SMC', pair, f'ошибка сканирования: {exc}')

    report.finish('SMC')
    candidates.sort(key=lambda c: (-c['score'], -c['rr']))
    return candidates


# ── Единый набор функций реестра стратегий (strategies/registry.py, 08.10.2026) ──
# Раньше эти куски жили ветками в общих модулях: сканирование и сборка сигнала —
# в bot.py, величины исполнения — в strategy_profile.py, разметка — в
# setup_geometry.py. Поведение не изменилось — его держат эталоны tests/golden/.

def scan(pairs, gate, client=None):
    return scan_for_setups(pairs, gate, client=client)


def build_signal(candidate):
    pair = candidate['pair']
    signal = candidate['signal']
    smc_info = signal['smc']
    signal['scan'] = {
        'confluence': smc_info['confluence'],
        'poi_type': smc_info['poi_type'],
        'factors': [k for k, ok in smc_info['factors'].items() if ok],
        'sweep': smc_info['sweep'],
        'rr_first': smc_info['rr_first'],
        'rr_final': smc_info['rr_final'],
    }
    log(f"\n[SMC] {pair}: зона {candidate['poi_type']}, "
        f"confluence {candidate['score']}, RR {candidate['rr']:.2f}")
    return signal, candidate.get('df_1h')


def profile():
    # Модуль параметров — заново при каждом вызове: тесты перезагружают его, и
    # схваченный при импорте адаптера был бы чужим (как и в strategy_profile).
    from strategies.smc import params as p
    return {'expiry_hours': p.PENDING_ORDER_MAX_HOURS, 'cooldown_hours': p.COOLDOWN_HOURS,
            'cost_limit_pct': p.MAX_ENTRY_COST_SHARE_PCT, 'max_hold_hours': p.MAX_POSITION_HOLD_HOURS,
            'drops_at_target': p.CANCEL_PENDING_AT_TARGET, 'fills_through_market': p.FILL_THROUGH_MARKET,
            'max_same_direction': p.MAX_SAME_DIRECTION}


def geometry(signal, g):
    from strategies import glossary
    smc = signal.get('smc') or {}
    g.band(smc.get('poi_bottom'), smc.get('poi_top'),
           glossary.poi_type(smc.get('poi_type')), main=True)
    # Имбаланс — вторая половина основания сделки. Он участвует в отборе, но на
    # графике его не было вовсе.
    g.band(smc.get('fvg_bottom'), smc.get('fvg_top'), 'имбаланс (FVG)')
    g.leg('начало движения', 'конец движения')
    # ОТ ЧЕГО СТРОИЛАСЬ СТРУКТУРА. Ордер-блок показывает, ГДЕ вход; пробитый
    # уровень и снятая ликвидность отвечают, ПОЧЕМУ вообще эта сторона.
    if smc.get('structure_level'):
        g.lines.append({
            'price': float(smc['structure_level']),
            'label': (glossary.structure_event(smc.get('structure_type'))
                      + ' · пробитый уровень'),
        })
    if smc.get('sweep_price'):
        g.lines.append({
            'price': float(smc['sweep_price']),
            'label': (glossary.liquidity_side(smc.get('sweep_side')) + ' · снята'),
        })
