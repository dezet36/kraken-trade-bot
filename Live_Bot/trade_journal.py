"""
Detailed trade journal — saves every trade to trades_journal.csv.
One row per trade, written at close. Tracks full lifecycle in-memory.
Open in Excel / Google Sheets for analysis.

Расширение 2026-07-05: ПОЛНЫЙ дамп каждой сделки дополнительно пишется в
trades_detail.jsonl (одна JSON-строка на сделку; мульти-тенант — в
state/<telegram_id>/trades_detail.jsonl). Содержит всё для разбора: контекст
скана (score/RR/близость), якоря импульса A/B со временами, границы зоны A,
уровень инвалидации, жизненный цикл входа (лимит/маркет, ожидание, слиппедж),
MFE/MAE с R-мультипликаторами, время безубытка. CSV-колонки легаси не меняются.
Анализ: pd.read_json('trades_detail.jsonl', lines=True).
"""

import csv
import json
import os
from datetime import datetime, timezone

import config
import csv_journal
from logger import log

JOURNAL_FILE  = os.path.join(config.DATA_DIR, 'trades_journal.csv')
DETAIL_JSONL  = os.path.join(config.DATA_DIR, 'trades_detail.jsonl')

COLUMNS = [
    # Идентификация
    # 'strategy' добавлена для параллельного A/B-режима: без неё сделки двух
    # стратегий смешиваются в одну кучу и месячное сравнение невозможно.
    # Колонка идёт в конце списка — старые CSV читаются без изменений.
    'trade_id', 'mode',
    # Открытие
    'open_time', 'pair', 'direction', 'zone', 'htf_trend',
    'impulse_pct', 'setup_high', 'setup_low',
    # Уровни
    'entry_price', 'stop_loss', 'tp1', 'tp2', 'tp3', 'tp4',
    'rr', 'leverage', 'risk_usd', 'position_size', 'balance_before',
    # Тейк-профиты (заполняются по мере отработки)
    'tp1_time', 'tp1_price',
    'tp2_time', 'tp2_price',
    'tp3_time', 'tp3_price',
    # Закрытие
    'close_time', 'exit_price', 'exit_reason',
    'tps_hit', 'pnl_usd', 'pnl_pct', 'duration_min',
    'balance_after',
    # Итог
    'result',       # WIN / LOSS / BREAKEVEN
    'setup_notes',  # почему открылась (краткое описание)
    'strategy',     # FIBO / SMC — какая стратегия открыла сделку
    # ── Издержки и всё, что от них считается (добавлено 30 августа 2026) ─────
    #
    # ЗАЧЕМ. До этой даты боевой журнал не знал про комиссии ВООБЩЕ, а pnl_usd
    # был грязным итогом. Бумажный двойник за август показал, чем всё
    # решается: грязный +$34.94, комиссии −$661.03, чистый −$616.77. Журнал
    # без этих колонок не мог ответить на единственный важный вопрос — что
    # именно съело результат, — и месяц наблюдений пропал бы впустую.
    #
    # Колонки идут В КОНЕЦ: старые CSV читаются без изменений, а недостающие
    # поля в прежних строках останутся пустыми. Так уже добавляли strategy.
    'gross_pnl_usd',   # до вычета издержек — чтобы видеть их вклад отдельно
    'fees_usd',        # комиссии за вход, частичные фиксации и выход
    'funding_usd',     # фандинг за удержание (плюс = заплатили)
    # ЗАМЕРЕНО ИЛИ ПОСЧИТАНО: 'exchange' — сумма удержаний с биржи, 'estimate' —
    # оценка по тарифу из config. Смешивать их в одной колонке и делать выводы
    # нельзя, поэтому источник записывается рядом с числом.
    'fees_source',
    'pnl_r',           # итог в риск-единицах: сравнимо между парами и плечами
    # Какую долю риска съели комиссии. Известна ЗАРАНЕЕ, при входе: зависит
    # только от тесноты стопа (доля = ставка_туда-обратно / стоп%).
    'cost_share_pct',
    # Насколько далеко цена уходила за нас и против нас, в R.
    # Считались и раньше, но до 30 августа терялись: DictWriter получает
    # extrasaction='ignore' и молча выбрасывал всё, чего нет в COLUMNS.
    # В trades_detail.jsonl они были, в CSV — нет, а панель читает CSV.
    'mfe_r', 'mae_r',
    # КОГДА цена дошла до этих точек, в минутах от входа. Без порядка событий
    # нельзя ответить, безубыток спасает или режет: разбор 29 августа упёрся
    # ровно в это — 21 сделка доходила до цели и закрылась в ноль.
    'mfe_min', 'mae_min',
    # Обстановка на входе: размах свечей в процентах цены и час суток UTC.
    # Без них нельзя спросить, в каком рынке стратегия работает.
    'atr_pct', 'hour_utc',
    # ── Разбор языковой модели (стратегия LLM) ──────────────────────────────
    #
    # Пустые у остальных четырёх стратегий, и это нормально: колонка,
    # осмысленная для одной стратегии, не повод заводить второй журнал.
    # Разъехавшиеся журналы этот проект уже проходил — боевой отстал от
    # бумажного на двенадцать колонок именно так.
    #
    # llm_analysis — разбор, который модель пишет ДО решения. Он и есть
    # главная ценность записи: по нему потом видно, на чём именно она
    # ошиблась, а не только что ошиблась.
    'llm_donor',        # чей сетап послужил поводом посмотреть на пару
    'llm_regime', 'llm_analysis', 'llm_trigger', 'llm_risk', 'llm_alt',
    # Вероятность отработки по мнению модели. Проверяется на калибровку:
    # выигрывают ли сетапы, названные «0.60», действительно в 60% случаев.
    'llm_p',
    'llm_votes',        # сколько факторов из пяти она отметила
    'llm_model',        # какой моделью получен ответ: сравнивать надо равное
]


def read_journal(path=None):
    """
    Все закрытые боевые сделки списком словарей.

    Появилось ради дневного стоп-крана: он считает потери за сегодня по
    журналу, а не по счётчику в памяти — счётчик обнуляется при перезапуске, и
    бот, поднятый посреди плохого дня, начинал бы его заново.

    Отказ чтения возвращает пустой список, а не бросает: журнал — не то, из-за
    чего стоит останавливать торговлю. Но и молчаливо считать «сегодня ничего
    не потеряно» нельзя, поэтому вызывающий обязан отличать пустой журнал от
    нечитаемого — здесь для этого пишется предупреждение.
    """
    path = path or JOURNAL_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            return list(csv.DictReader(f))
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ Журнал сделок не прочитан ({exc}) — дневной предел убытка '
            f'считает по неполным данным')
        return []


def _next_trade_id() -> int:
    """Returns next sequential trade ID based on existing rows."""
    if not os.path.exists(JOURNAL_FILE):
        return 1
    with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 1
    try:
        return max(int(r.get('trade_id', 0)) for r in rows) + 1
    except Exception:
        return len(rows) + 1


def open_trade(position: dict, signal: dict, balance_before: float) -> int:
    """
    Called when trade opens. Populates position dict with journal fields.
    Returns the assigned trade_id.
    """
    setup   = signal['setup']
    trigger = signal['trigger']
    params  = signal['params']

    trade_id = _next_trade_id()

    # Store journal metadata inside position for later use
    position['_journal'] = {
        'trade_id':     trade_id,
        'strategy':     signal.get('strategy', 'FIBO'),
        'open_time':    datetime.now().isoformat(timespec='seconds'),
        'pair':         signal['trading_pair'],
        'direction':    setup['type'],
        'zone':         trigger['zone'],
        'htf_trend':    signal.get('htf_trend', 'N/A'),
        'impulse_pct':  round(setup['size'] / setup['end_price'] * 100, 2),
        'setup_high':   round(setup['end_price'] if setup['type'] == 'LONG' else setup['start_price'], 6),
        'setup_low':    round(setup['start_price'] if setup['type'] == 'LONG' else setup['end_price'], 6),
        'entry_price':  params['entry'],
        'stop_loss':    params['stop_loss'],
        'tp1':          params['take_profit_1'],
        'tp2':          params['take_profit_2'],
        'tp3':          params.get('take_profit_3', ''),
        'tp4':          params.get('take_profit_4', ''),
        'rr':           params['rr'],
        'leverage':     params.get('leverage', 'N/A'),
        'risk_usd':     params['risk_amount'],
        'position_size': params['position_size'],
        'balance_before': balance_before,
        # TP hit tracking
        'tp1_time': '', 'tp1_price': '',
        'tp2_time': '', 'tp2_price': '',
        'tp3_time': '', 'tp3_price': '',
        # ── Расширенный контекст (2026-07-05): «почему открылась» ────────────
        'setup_start_time': str(setup.get('start_time', '')),   # время якоря A
        'setup_end_time':   str(setup.get('end_time', '')),     # время якоря B
        'zone_a_top':       (signal.get('zone_a') or {}).get('top', ''),
        'zone_a_bottom':    (signal.get('zone_a') or {}).get('bottom', ''),
        # инвалидация сетапа = дальняя граница зоны B (88.6%-уровень)
        'invalidation':     ((signal.get('zone_b') or {}).get('bottom', '')
                             if setup['type'] == 'LONG'
                             else (signal.get('zone_b') or {}).get('top', '')),
        'be_level':         params.get('be_level', ''),
        'session_hour_utc': datetime.now(timezone.utc).hour,
        # Тот же час под именем hour_utc — так колонка называется в бумажном
        # журнале. Имена ОДНОГО И ТОГО ЖЕ должны совпадать в обоих журналах:
        # разойдясь, они заставляют разбор писать две ветки под одно понятие,
        # и рано или поздно одну из них забывают.
        'hour_utc':         datetime.now(timezone.utc).hour,
        # Размах свечей в процентах цены на момент решения. None, если свечей
        # не хватило — пустая клетка честнее выдуманного числа.
        'atr_pct':          signal.get('atr_pct'),
        # контекст скана: score и компоненты (bot.py/platform_manager кладут в signal)
        **{f'scan_{k}': v for k, v in (signal.get('scan') or {}).items()},
    }
    return trade_id


def record_tp_hit(position: dict, tp_num: int, price: float):
    """Called when each TP level is hit."""
    j = position.get('_journal')
    if not j or tp_num > 3:
        return
    ts = datetime.now().isoformat(timespec='seconds')
    j[f'tp{tp_num}_time']  = ts
    j[f'tp{tp_num}_price'] = round(price, 6)


def close_trade(position: dict, exit_price: float, exit_reason: str,
                pnl_usd: float, balance_after: float, import_config, telegram_id=None,
                gross_pnl=None, costs=None):
    """
    Вызывается при закрытии сделки. Пишет полную строку:
    - telegram_id задан (мульти-тенант) -> в БД (db.record_trade)
    - иначе -> в общий CSV (legacy одно-юзер)
    Возвращает построенную строку (dict) либо None.

    pnl_usd — ЧИСТЫЙ итог, уже за вычетом издержек. gross_pnl и costs приходят
    оттуда же (live_costs.settle) и записываются рядом, чтобы вклад комиссий
    был виден отдельно, а не только в разнице. Оба необязательны: без них
    строка пишется как раньше, а колонки издержек остаются пустыми — так
    вызовы из старых мест не падают.
    """
    j = position.get('_journal')
    if not j:
        return None

    open_dt  = datetime.fromisoformat(j['open_time'])
    close_dt = datetime.now()
    duration = int((close_dt - open_dt).total_seconds() / 60)

    balance_before = j['balance_before']
    pnl_pct = round(pnl_usd / balance_before * 100, 2) if balance_before else 0

    if pnl_usd > 0:
        result = 'WIN'
    elif pnl_usd < 0:
        result = 'LOSS'
    else:
        result = 'BREAKEVEN'

    tps_hit = position.get('tp_hit', 0)

    # Build human-readable setup note
    direction = j['direction']
    zone      = j['zone']
    htf       = j['htf_trend']
    imp       = j['impulse_pct']
    rr        = j['rr']
    recovered = bool(position.get('recovered'))
    notes = ((f"[ВОССТАНОВЛЕНО после рестарта бота — impulse/zone приближённые] "
              if recovered else "") +
             f"{direction} в {zone}, HTF={htf}, "
             f"импульс={imp}%, RR=1:{rr}, "
             f"выход={exit_reason}")

    row = {
        'trade_id':     j['trade_id'],
        'mode':         import_config.TRADING_MODE,
        'open_time':    j['open_time'],
        'pair':         j['pair'],
        'direction':    direction,
        'zone':         zone,
        'htf_trend':    htf,
        'impulse_pct':  imp,
        'setup_high':   j['setup_high'],
        'setup_low':    j['setup_low'],
        'entry_price':  j['entry_price'],
        'stop_loss':    j['stop_loss'],
        'tp1':          j['tp1'],
        'tp2':          j['tp2'],
        'tp3':          j['tp3'],
        'tp4':          j['tp4'],
        'rr':           rr,
        'leverage':     j['leverage'],
        'risk_usd':     j['risk_usd'],
        'position_size': j['position_size'],
        'balance_before': balance_before,
        'tp1_time':     j['tp1_time'],
        'tp1_price':    j['tp1_price'],
        'tp2_time':     j['tp2_time'],
        'tp2_price':    j['tp2_price'],
        'tp3_time':     j['tp3_time'],
        'tp3_price':    j['tp3_price'],
        'close_time':   close_dt.isoformat(timespec='seconds'),
        'exit_price':   round(exit_price, 6),
        'exit_reason':  exit_reason,
        'tps_hit':      tps_hit,
        'pnl_usd':      round(pnl_usd, 2),
        'pnl_pct':      pnl_pct,
        'duration_min': duration,
        'balance_after': round(balance_after, 2),
        'result':       result,
        'setup_notes':  notes,
    }

    # ── Расширенный дамп (2026-07-05): «как отработала» ──────────────────────
    # Все дополнительные j-поля (setup_*, zone_a_*, invalidation, scan_*, ...)
    for k, v in j.items():
        if k not in row:
            row[k] = v

    entry = float(j['entry_price'] or 0)
    sl    = float(j['stop_loss'] or 0)
    sl_dist = abs(entry - sl)
    mfe = position.get('mfe_price')
    mae = position.get('mae_price')
    if mfe is not None and mae is not None and sl_dist > 0:
        sign = 1 if direction == 'LONG' else -1
        row['mfe_price'] = round(mfe, 6)
        row['mae_price'] = round(mae, 6)
        row['mfe_r'] = round(sign * (mfe - entry) / sl_dist, 3)   # макс. ход ЗА нас, в R
        row['mae_r'] = round(sign * (mae - entry) / sl_dist, 3)   # макс. ход ПРОТИВ нас, в R
    # ── Издержки и производные от них ────────────────────────────────────────
    if costs:
        row['fees_usd'] = costs.get('fees_usd')
        row['funding_usd'] = costs.get('funding_usd')
        row['fees_source'] = costs.get('fees_source')
    if gross_pnl is not None:
        row['gross_pnl_usd'] = round(gross_pnl, 4)

    # Итог в риск-единицах. Только так сравнимы сделки по разным парам и с
    # разным плечом: доллары у BTC и SHIB значат разное, а R — одно и то же.
    risk_usd = float(j.get('risk_usd') or 0)
    if risk_usd > 0:
        row['pnl_r'] = round(pnl_usd / risk_usd, 3)

    # Доля риска, съеденная комиссиями. Зависит только от тесноты стопа и
    # потому известна ещё при входе — записываем, чтобы можно было проверить
    # на НОВЫХ данных догадку, что дешёвые входы прибыльнее дорогих.
    if sl_dist > 0 and entry > 0:
        import risk_gate
        share = risk_gate.entry_cost_share(entry, sl_dist,
                                           import_config.ENTRY_COST_ROUND_TRIP)
        row['cost_share_pct'] = round(share * 100, 2)

    # Когда цена дошла до лучшей и худшей точки, в минутах от входа.
    #
    # ОТСЧЁТ ОТ entry_time ПОЗИЦИИ, А НЕ ОТ open_time ЖУРНАЛА. В бою эти два
    # момента отличаются на доли секунды — их ставят соседние строки кода, — но
    # это ДВА РАЗНЫХ ИСТОЧНИКА ВРЕМЕНИ, и отметки экстремумов ставит первый из
    # них. Смешав их, получаем разность между чужими часами: на проверке это
    # дало −579 минут, то есть цена «дошла до максимума» за девять часов до
    # собственного входа. В журнале такое число выглядело бы просто странным.
    started = position.get('entry_time') or open_dt
    for key, column in (('mfe_ts', 'mfe_min'), ('mae_ts', 'mae_min')):
        moment = position.get(key)
        if moment is not None:
            row[column] = int((moment - started).total_seconds() / 60)

    # Разбор модели. Берётся из ОДНОЙ функции с бумажным журналом: колонки,
    # заполненные дважды, разойдутся.
    import paper_broker
    row.update(paper_broker._llm_columns(
        (position.get('signal') or {}).get('llm')))

    row['be_time'] = position.get('be_time', '')
    row['breakeven_set'] = bool(position.get('breakeven_set'))
    row['recovered'] = recovered
    for k, v in (position.get('_lifecycle') or {}).items():
        row[f'entry_{k}'] = v
    if row.get('entry_placed_at') and row.get('entry_filled_at'):
        try:
            row['entry_wait_min'] = int((datetime.fromisoformat(row['entry_filled_at'])
                                         - datetime.fromisoformat(row['entry_placed_at']))
                                        .total_seconds() / 60)
        except Exception:
            pass

    if telegram_id is not None:
        import db
        db.record_trade(telegram_id, row)   # полный row уходит в data-JSON
        detail_path = os.path.join(config.DATA_DIR, 'state',
                                   str(telegram_id), 'trades_detail.jsonl')
    else:
        csv_journal.migrate_header(JOURNAL_FILE, COLUMNS, 'боевой журнал')
        write_header = not os.path.exists(JOURNAL_FILE) or os.path.getsize(JOURNAL_FILE) == 0
        with open(JOURNAL_FILE, 'a', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction='ignore')
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        detail_path = DETAIL_JSONL

    # JSONL с ПОЛНЫМ дампом — в обоих режимах (сбой записи не роняет закрытие сделки)
    try:
        os.makedirs(os.path.dirname(detail_path), exist_ok=True)
        with open(detail_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass
    return row
