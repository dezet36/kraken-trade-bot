import os
import sys
import time
from datetime import datetime, date, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler

import config
import telegram_notify as tg
from telegram_bot import controller
from exchange import get_exchange, make_market_client
import dashboard
import error_log
import positioning
import market_cap
import market_mood
import strategy_levels
import strategy_llm
import strategy_rsibb
import strategy_smc
import strategy_smcs
import strategy_fib12
from strategy import analyze_market
from pair_scanner import get_liquid_pairs, scan_for_setups
from paper_broker import PaperBroker, STRATEGIES as PAPER_STRATEGIES
# Деньги и допуск стратегий — у их тестовых счетов (реорганизация, этап 2).
from accounts import paper as account
from accounts import trading as trading_accounts
from trade_manager import LiveTradeManager
from logger import log

trade_manager   = None
broker          = None      # фантомный счёт (TRADING_MODE=PAPER)
_last_summary_date = None   # tracks date of last daily summary sent


def _send_daily_summary_if_needed():
    """Sends daily summary once per day at the first cycle after midnight."""
    global _last_summary_date
    if trade_manager is None and broker is not None:
        # Бумажный счёт: итог — по журналу, раз в сутки UTC, дата отправки на
        # диске (telegram_state), поэтому спрашивать можно каждый цикл.
        # Прежний путь ниже брал сделки из памяти процесса и помнил день там
        # же: после каждой выкатки сводка уходила заново и говорила «сделок
        # не было».
        tg.daily_report_once(broker)
        return

    today = date.today()
    if _last_summary_date == today:
        return
    _last_summary_date = today

    executor = trade_manager if trade_manager is not None else broker
    if executor is None:
        return

    # Collect yesterday's closed trades from trade_history
    history      = executor.trade_history
    yesterday    = today - timedelta(days=1)
    today_trades = [t for t in history
                    if t.get('exit_time') and t['exit_time'].date() == yesterday]
    # Сводка с разбивкой по стратегиям. Общая складывала всё в одну кучу —
    # «12 сделок, +$40», — и по такой строке нельзя понять, что одна стратегия
    # заработала, а вторая ровно столько же потеряла. Ради этого сравнения
    # фантом и запущен.
    rows = [{'strategy': t.get('strategy') or '—',
             'pnl': t.get('pnl', 0) or 0,
             'pnl_r': t.get('pnl_r', 0) or 0}
            for t in today_trades]
    tg.daily_by_strategy(rows, yesterday.strftime('%d.%m.%Y'))


def _recorded_pairs(base):
    """
    Пары для записи сырых данных (стакан, лента, ликвидации, ОИ): список бота
    и своя вселенная стратегии, если она шире — у ИИ по тетради 30 пар против
    ~20 ликвидных. Стакан и ленту задним числом не скачать: что не записано
    сегодня, потеряно. Только добавление: по парам бота пишется всё как было.
    """
    extra = []
    try:
        import llm_notebook
        if llm_notebook.enabled():
            extra = list(llm_notebook.UNIVERSE)
    except Exception:                                  # noqa: BLE001
        pass
    return list(dict.fromkeys(list(base or []) + extra))


def _build_signal(candidate, strategy, balance, setups=None):
    """
    Достраивает из кандидата сканера готовый сигнал.

    Возвращает (signal, df_1h) либо (None, None). Контекст «почему открылась»
    кладётся в signal['scan'] — он же уходит в журнал и в дашборд, поэтому по
    закрытой сделке потом видно, на каком основании в неё зашли.

    ВЕТКА ПОД КАЖДУЮ СТРАТЕГИЮ ОБЯЗАТЕЛЬНА, И ВОТ ПОЧЕМУ. Раньше здесь стояло
    «если SMC — так, ИНАЧЕ — фибо». Стратегия уровней не подходила ни под одно
    условие и уходила в ветку фибо: её готовый сигнал ВЫБРАСЫВАЛСЯ, а вместо
    него на тех же свечах заново искался импульс Фибоначчи. Дальше результат
    помечался именем LEVELS. То есть уровни либо не торговали вовсе, либо
    торговали чужой сетап под своим именем — и в журнале это выглядело как
    нормальная работа стратегии уровней.

    Поэтому неизвестная стратегия теперь ОТКАЗЫВАЕТСЯ обслуживаться, а не
    достаётся фибо по умолчанию. Четвёртая стратегия, добавленная когда-нибудь
    позже, упрётся в явный отказ в журнале вместо тихой подмены.
    """
    pair = candidate['pair']
    # С 08.10.2026 сборку ведёт адаптер стратегии из реестра (strategies/
    # registry.py): у каждой своя функция build_signal, общих веток здесь нет.
    from strategies import registry
    if registry.get(strategy) is None:
        log(f"   {strategy}: нет ветки сборки сигнала — вход отменён. "
            f"Молча отдать кандидата чужой стратегии нельзя: она откроет "
            f"свой сетап под этим именем.")
        return None, None
    signal, df_for_chart = registry.adapter(strategy).build_signal(candidate)
    if not signal:
        return None, None

    # Копия сетапа без денег — торговым счетам (accounts/trading.py: проп по
    # инструкциям, биржи), до решения тестового счёта: у них свои стороны и
    # свой риск. Её сбой сделку теста не трогает.
    if setups is not None:
        try:
            setups.append(trading_accounts.setup_copy(signal))
        except Exception as exc:                       # noqa: BLE001
            log(f"   ⚠️ {strategy} {pair}: копия сетапа для торговых счетов — {exc}")

    # Решение счёта стратегии (accounts/paper.py, с 08.10.2026): разрешена ли
    # сторона и на каких деньгах — риск, предел в одну сторону, размер.
    # Стратегия отдаёт сетап без денег. Проверка сторон стоит ЗДЕСЬ, после
    # сборки сигнала, а не в сканере: направление известно только у готового
    # сетапа, и каждая стратегия называет его по-своему. Отказ пишется в
    # журнал — иначе выключенное направление выглядит как «бот перестал
    # находить сетапы».
    signal, why = account.decide(strategy, signal, balance)
    if signal is None:
        log(f"   {strategy} {pair}: {why}")
        return None, None

    signal['strategy'] = strategy
    return signal, df_for_chart


def _trading_wants(strategy):
    """Ждёт ли сетапы стратегии хоть один торговый счёт. Сбой — нет."""
    try:
        return trading_accounts.wants(strategy)
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ торговые счета: {exc}')
        return False


def _offer_to_trading(strategy, setup=None, candidate=None):
    """
    Сетап — торговым счетам (accounts/trading.py). Кандидат сверх предела
    заявок теста достраивается здесь: адаптер строит сигнал из готового
    кандидата, без сети. Отказ или сбой торговых счетов тест не трогает.
    """
    try:
        if setup is None:
            from strategies import registry
            signal, _ = registry.adapter(strategy).build_signal(candidate)
            if not signal:
                return
            setup = trading_accounts.setup_copy(signal)
        trading_accounts.offer(strategy, setup)
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ торговые счета: {strategy} — {exc}')


def _open_from_candidate(candidate, strategy, balance):
    """
    Открывает сделку по кандидату конкретной стратегии.

    Возвращает True, если позиция реально открыта. Разметку стратегии кладём
    прямо в сигнал: trade_manager сохранит её в карту пар и в журнал, и после
    рестарта будет понятно, чья это позиция.
    """
    signal, df_for_chart = _build_signal(candidate, strategy, balance)
    if signal is None:
        return False
    return bool(trade_manager.execute_trade(signal, df_1h=df_for_chart))


def _run_dual_strategy(liquid_pairs, balance, open_pairs):
    """
    Параллельный режим: обе стратегии торгуют одновременно.

    У каждой свой бюджет слотов, поэтому исчерпание лимита одной не мешает
    другой — иначе более частая фибо-стратегия просто вытеснила бы SMC, и
    сравнение потеряло бы смысл.

    Конфликт по паре неизбежен: на бирже по инструменту может быть только
    одна позиция. Кто первый — того и пара; вторая стратегия пропускает и
    это пишется в лог, чтобы при разборе итогов знать масштаб перекоса.
    """
    taken = set(open_pairs)
    total_opened = 0

    # Стратегии боевого цикла — из реестра (ИИ торгуется только на бумаге).
    from strategies import registry
    scanners = [(code, (lambda code=code: registry.adapter(code).scan(
        liquid_pairs, trade_manager, client=None)))
        for code in registry.live_codes()]

    for strategy, scan in scanners:
        if not account.enabled(strategy):
            log(f"\n=== {strategy}: выключена оператором, пропускаем ===")
            continue

        used = trade_manager.slots_used_by(strategy)
        free = account.slots_free(strategy, used)
        log(f"\n=== {strategy}: занято {account.slots_label(strategy, used)} слотов ===")
        if free is not None and free <= 0:
            log(f"   {strategy}: слоты заняты, пропускаем")
            continue

        # Блок-лист часов входа — фильтр самой ФИБО: он в её адаптере (scan).
        try:
            candidates = scan()
        except Exception as exc:
            log(f"   {strategy}: ошибка сканирования — {exc}")
            tg.error_alert(f"{strategy}: ошибка сканирования — {exc}")
            continue

        log(f"   {strategy}: сетапов найдено {len(candidates)}")
        opened = 0
        for candidate in candidates:
            if free is not None and opened >= free:
                break
            pair = candidate['pair']
            if pair in taken:
                log(f"   {strategy}: {pair} уже занята другой стратегией, пропуск")
                continue
            try:
                if _open_from_candidate(candidate, strategy, balance):
                    opened += 1
                    total_opened += 1
                    taken.add(pair)
            except Exception as exc:
                log(f"   {strategy}: ошибка входа {pair} — {exc}")
                tg.error_alert(f"{strategy}: ошибка входа {pair} — {exc}")

        log(f"   {strategy}: открыто {opened}")

    log(f"\nИтог цикла: открыто новых сделок — {total_opened}")


def _paper_cycle():
    """
    Фантомный цикл: ни одного ордера на биржу.

    Отличие от боевого цикла ровно одно, но принципиальное — занятость пары
    считается для каждой стратегии отдельно. Обе могут одновременно держать
    BTCUSDT, в том числе в разные стороны. На бирже так нельзя, поэтому для
    сравнения стратегий это единственный способ не дать одной отбирать сетапы
    у другой.
    """
    # Сначала прокручиваем уже открытое: ордера заполняются, стопы и тейки
    # срабатывают. Делаем это ДО проверки паузы — пауза запрещает новые входы,
    # а не ведение позиций.
    broker.update()
    # Торговые счета (проп по инструкциям, биржи): свечи, события, сверка с
    # биржей, правила пропа — до паузы, как и тест: пауза запрещает новые
    # входы, а не ведение.
    try:
        trading_accounts.update(broker.client)
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ торговые счета: {exc}')

    if controller.is_paused():
        log("⏸ Бот на паузе — новые фантомные входы пропускаем")
        # Сбор позиционирования идёт и на паузе: пауза останавливает
        # сделки, а не наблюдение за рынком.
        positioning.collect_if_due(broker.client, pairs=_recorded_pairs(config.TRADING_PAIRS_POOL))
        market_cap.collect_if_due(broker.client)
        market_mood.collect_if_due()
        _poll_news()
        return

    client = broker.client
    try:
        liquid_pairs = get_liquid_pairs(client)
    except Exception as exc:
        log(f"Ошибка получения ликвидных пар: {exc}")
        return
    if not liquid_pairs:
        log("Нет ликвидных пар — пропускаем цикл")
        return

    # Ликвидации приходят потоком, а не по запросу: поток поднимается один
    # раз и дальше только пополняет список пар. Падение потока бот не
    # трогает — без него всё торгует как вчера.
    recorded = _recorded_pairs(liquid_pairs)
    try:
        import liquidations
        liquidations.ensure_running(recorded, client)
    except Exception as exc:                           # noqa: BLE001
        log(f'   ликвидации: сборщик не запущен — {exc}')
    # Лента сделок — тоже потоком: опрос покрывал у BTC 28% минут.
    try:
        import trades_ws
        trades_ws.ensure_running(recorded, client)
    except Exception as exc:                           # noqa: BLE001
        log(f'   лента сделок: сборщик не запущен — {exc}')
    _watch_streams()

    # Режим рынка по BTC — в журнал сетапов всех стратегий: брокер кладёт его
    # в контекст заявки (market_regime.last_btc_regime), сам к бирже не ходит.
    # Раз в сутки UTC; запрос тот же, что у SMC, и берётся из кэша свечей.
    try:
        import market_regime
        from exchange import fetch_ohlcv
        market_regime.btc_regime(
            lambda tf, limit, sym: fetch_ohlcv(tf, limit=limit, symbol=sym, client=client))
    except Exception as exc:                           # noqa: BLE001
        log(f'   режим рынка для журнала не посчитан — {exc}')

    total_opened = 0
    # Кандидаты этого цикла по стратегиям: их разбирает модель, когда доходит
    # очередь до пятой стратегии. Сканировать пул заново ради неё значило бы
    # повторить всю сетевую работу второй раз за цикл.
    found = {}

    for strategy in broker.strategies:
        if not account.enabled(strategy):
            log(f"\n=== {strategy}: выключена оператором, пропускаем ===")
            continue

        balance = broker.balance(strategy)
        used = broker.slots_used_by(strategy)
        free = account.slots_free(strategy, used)
        equity = broker.equity(strategy)
        start = broker.start_balance(strategy)
        growth = (equity / start - 1) * 100 if start else 0.0

        log(f"\n=== {strategy}: депозит ${equity:,.2f} ({growth:+.2f}%) | "
            f"слотов {account.slots_label(strategy, used)} ===")
        if free is not None and free <= 0:
            log(f"   {strategy}: слоты заняты, пропускаем")
            continue
        if balance <= 0:
            log(f"   {strategy}: депозит обнулён, торговля остановлена")
            continue

        gate = broker.gate(strategy)
        # Сканер — адаптер стратегии из реестра (strategies/registry.py). Чужого
        # сканера по умолчанию нет: стратегия без записи в реестре не торгует —
        # иначе она молча получала бы чужих кандидатов и торговала их под своим
        # именем (так однажды стоило месяца недостоверных наблюдений уровней).
        # Блок-лист часов входа ФИБО — в её адаптере.
        from strategies import registry
        if registry.get(strategy) is None:
            log(f"   {strategy}: нет сканера — пропускаем. Отдать пул "
                f"чужому сканеру нельзя: он найдёт свои сетапы и они "
                f"уйдут в журнал под этим именем.")
            continue
        try:
            candidates = registry.adapter(strategy).scan(liquid_pairs, gate, client=client)
        except Exception as exc:
            log(f"   {strategy}: ошибка сканирования — {exc}")
            continue

        found[strategy] = candidates
        log(f"   {strategy}: сетапов найдено {len(candidates)}")
        # Счета по инструкциям, выбравшие стратегию, получают сетапы ЭТОГО
        # прохода — копией без денег, до решения тестового счёта. Тест не
        # меняется: те же кандидаты в том же порядке и тот же предел заявок.
        trading = _trading_wants(strategy)
        opened = 0
        for candidate in candidates:
            if free is not None and opened >= free:
                if not trading:
                    break
                _offer_to_trading(strategy, candidate=candidate)
                continue
            try:
                setups = [] if trading else None
                signal, _ = _build_signal(candidate, strategy, balance, setups=setups)
                if setups:
                    _offer_to_trading(strategy, setup=setups[0])
                if signal and broker.open(strategy, signal):
                    opened += 1
                    total_opened += 1
            except Exception as exc:
                log(f"   {strategy}: ошибка входа {candidate['pair']} — {exc}")

        log(f"   {strategy}: поставлено ордеров — {opened}")

    log(f"\nИтог цикла: новых фантомных ордеров — {total_opened}")

    # Сбор данных о позиционировании: раз в час, молча, и НИКОГДА не мешая
    # торговле — все ошибки гасятся внутри. Данные копятся впрок, потому что
    # биржа отдаёт их с пределом по числу записей, и медвежий период 2022-23
    # недостижим никаким запросом.
    #
    # В КОНЦЕ ЦИКЛА, А НЕ В НАЧАЛЕ. Сбор — это 84 запроса к бирже разом, и
    # в цикле, что приходится на начало часа, он шёл прямо перед сканерами
    # — те упирались в предел запросов, и пары выпадали из просмотра.
    positioning.collect_if_due(broker.client, pairs=_recorded_pairs(config.TRADING_PAIRS_POOL))
    # Рынок в целом (USDT.D, BTC.D, TOTAL2): один запрос тикеров спота.
    market_cap.collect_if_due(broker.client)
    # Настроение рынка (DVOL, премия Coinbase у BTC, доля спота): четыре запроса раз в закрытый час.
    market_mood.collect_if_due()
    # Объявления биржи: раз в час, только запись.
    _poll_news()


def _poll_news():
    """
    Объявления Bybit — сырые данные, пока только запись (news_feed). До
    08.10.2026 их опрашивала тетрадь ИИ внутри своего часа; сбор данных — дело
    цикла, а не стратегии (реорганизация, этап 4). Раз в час — свой предел у
    news_feed.poll; отказ — строка в журнал, торговля от него не зависит.
    """
    try:
        import news_feed
        news_feed.poll()
    except Exception as exc:                           # noqa: BLE001
        log(f'   новости биржи не записаны — {exc}')


_CYCLE_STAMP = os.path.join(config.DATA_DIR, 'last_cycle.json')

# Простой длиннее этого — событие, о котором надо сказать вслух: главная
# беда проекта — непрерывность 14%, и каждая дыра портит журнал молча.
CONTINUITY_ALERT_MIN = int(os.getenv('CONTINUITY_ALERT_MIN', 30))


def note_cycle():
    """
    Отмечает цикл и сообщает о простое, если прошлый был давно.

    Метка живёт в файле, а не в памяти: простой — это как раз то время, когда
    процесса не было, и знать о нём может только диск. Сообщение одно на
    простой, при первом цикле после него.
    """
    import json
    now = time.time()
    try:
        with open(_CYCLE_STAMP, encoding='utf-8') as fh:
            previous = float(json.load(fh).get('ts') or 0)
    except (OSError, ValueError):
        previous = 0.0
    try:
        with open(_CYCLE_STAMP, 'w', encoding='utf-8') as fh:
            json.dump({'ts': now}, fh)
    except OSError:
        pass
    if previous and now - previous > CONTINUITY_ALERT_MIN * 60:
        minutes = int((now - previous) / 60)
        log(f"⚠️ Простой {minutes} мин: свечи за это время в журнал не попали, "
            f"сделки, растянувшиеся через дыру, помечены data_gap_min")
        tg.error_alert(f"Бот не работал {minutes} мин (с "
                       f"{datetime.fromtimestamp(previous).strftime('%d.%m %H:%M')}). "
                       f"Непрерывность нарушена — сделки через дыру помечены в журнале.")


_stream_alerted = {}


# Сколько минут тишины считать обрывом. Сделки по двадцати парам идут
# непрерывно — десять минут без единой означают обрыв. Ликвидации — событие
# редкое: ночью 19-20 сентября их не было по 10-15 минут при живом потоке,
# и предупреждение срабатывало на спокойный рынок.
STREAM_SILENCE_MIN = {'лента сделок': 10, 'ликвидации': 90}


def _watch_streams():
    """
    Потоки биржи молчат дольше положенного — сообщение, не чаще раза в час.

    Молчание потока неотличимо от спокойного рынка, если не смотреть на
    возраст последнего события; порог свой у каждого потока.
    """
    now = time.time()
    checks = []
    try:
        import trades_ws
        s = trades_ws.stats()
        checks.append(('лента сделок', s.get('last_event') or 0, s.get('since')))
    except Exception:                                  # noqa: BLE001
        pass
    try:
        import liquidations
        s = liquidations.stats()
        checks.append(('ликвидации', s.get('last_event') or 0, s.get('since')))
    except Exception:                                  # noqa: BLE001
        pass
    for name, last_ms, since in checks:
        if not since or now - since / 1000 < 600:
            continue                                   # только что запущен
        silent_min = (now - last_ms / 1000) / 60 if last_ms else (now - since / 1000) / 60
        if silent_min < STREAM_SILENCE_MIN.get(name, 10):
            continue
        if now - _stream_alerted.get(name, 0) < 3600:
            continue
        _stream_alerted[name] = now
        log(f"⚠️ {name}: событий нет {silent_min:.0f} мин — поток, вероятно, оборван")
        tg.error_alert(f"{name}: событий нет {silent_min:.0f} мин — поток биржи молчит, "
                       f"переподключение идёт само; если не восстановится — смотреть журнал.")


def trading_cycle():
    note_cycle()
    if config.PAPER_MODE:
        log("\n" + "=" * 60)
        log(f"ФАНТОМНЫЙ ЦИКЛ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log("=" * 60)
        _send_daily_summary_if_needed()
        _paper_cycle()
        return

    log("\n" + "=" * 60)
    log(f"ЦИКЛ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    # ── Дневной итог при смене даты ────────────────────────────────────────
    _send_daily_summary_if_needed()

    # ── Пауза ─────────────────────────────────────────────────────────────────
    if controller.is_paused():
        log("⏸ Бот на паузе — новые входы пропускаем (позиции управляются)")
        trade_manager.manage_active_positions()
        return

    balance = trade_manager.get_real_balance()
    log(f"Баланс: ${balance:.2f}")

    # ── Управление открытыми позициями ────────────────────────────────────
    trade_manager.manage_active_positions()

    # ── Проверяем отложенные GTC ордера ───────────────────────────────────
    trade_manager.check_pending_orders()

    # ── Что было с ценой после наших выходов ──────────────────────────────
    # Отдельным шагом, а не внутри скана: свечи разбирают стратегии, и тянуть
    # наблюдения через четыре сканера значило бы связать их с логикой, которая
    # к ним отношения не имеет. Пар под наблюдением единицы — те, где сделка
    # закрылась меньше 12 часов назад.
    try:
        import follow_up
        seen = follow_up.advance_live()
        if seen:
            log(f"Наблюдений после выхода досмотрено: {seen}")
    except Exception as e:                             # noqa: BLE001
        log(f"⚠️ Наблюдения после выхода не продвинулись: {e}")

    # Пересчитываем слоты после возможных заполнений pending ордеров
    active_count = trade_manager.get_active_count()
    open_pairs   = trade_manager.get_open_pairs()
    available    = config.MAX_ACTIVE_PAIRS - active_count

    log(f"Позиций: {active_count}/{config.MAX_ACTIVE_PAIRS} | Свободно: {available}")
    if open_pairs:
        log(f"Открытые пары: {', '.join(open_pairs)}")

    if available <= 0 and config.STRATEGY != 'BOTH':
        log("Все слоты заняты — новые входы пропускаем")
        return

    # ── Сессионный фильтр: в блок-часы UTC новые сетапы не открываем ──────
    # (позиции и pending уже обслужены выше — блокируется только скан/вход)
    # Фильтр откалиброван под ФИБО-стратегию (убыточность сетапов 12-16 UTC).
    # У SMC своя модель времени — killzones (§11.2), поэтому к ней этот
    # блок-лист не применяется: два фильтра времени подряд резали бы вход дважды.
    cur_hour_utc = datetime.now(timezone.utc).hour
    if config.STRATEGY == 'FIBO' and cur_hour_utc in config.BLOCK_ENTRY_HOURS_UTC:
        log(f"⏰ Сессионный фильтр: {cur_hour_utc:02d}:xx UTC в блок-листе "
            f"({sorted(config.BLOCK_ENTRY_HOURS_UTC)}) — новые входы пропускаем")
        return

    # ── Сканирование пула ─────────────────────────────────────────────────
    exchange = get_exchange()

    log(f"\nСканирую пул ({len(config.TRADING_PAIRS_POOL)} пар)...")
    try:
        liquid_pairs = get_liquid_pairs(exchange)
    except Exception as e:
        msg = f"Ошибка получения ликвидных пар: {e}"
        log(msg)
        tg.error_alert(msg)
        return

    log(f"Ликвидных пар: {len(liquid_pairs)}")

    if not liquid_pairs:
        log("Нет ликвидных пар — пропускаем цикл")
        return

    if config.STRATEGY == 'BOTH':
        _run_dual_strategy(liquid_pairs, balance, open_pairs)
        return

    if not account.enabled(config.STRATEGY):
        log(f"{config.STRATEGY}: выключена оператором — новые входы пропускаем")
        return

    if config.STRATEGY == 'SMC':
        log("\nПоиск SMC-сетапов (bias 1D/4H -> зоны 1H)...")
        candidates = strategy_smc.scan_for_setups(liquid_pairs, trade_manager)
    else:
        log("\nПоиск активных сетапов на 1H...")
        candidates = scan_for_setups(liquid_pairs, trade_manager)
    log(f"Сетапов найдено: {len(candidates)}")

    # Уведомление в Telegram только если есть кандидаты
    if candidates:
        tg.scan_result(len(liquid_pairs), len(candidates), active_count)

    if not candidates:
        log("Сетапов нет — ждём следующего цикла")
        return

    # ── Входы по топ-кандидатам ───────────────────────────────────────────
    signals_found = 0
    for candidate in candidates[: available * 2]:
        if signals_found >= available:
            break

        pair = candidate['pair']
        if pair in open_pairs:
            continue

        try:
            if config.STRATEGY == 'SMC':
                # SMC-сканер уже вернул готовый сигнал: повторный анализ той же
                # свечи ничего не уточнит, а лишний запрос к бирже сделает.
                signal = candidate['signal']
                log(f"\nВход в зону {candidate['poi_type']}: {pair} "
                    f"(confluence {candidate['score']}, RR {candidate['rr']:.2f})")
            else:
                log(f"\nПроверяю вход в зону A: {pair} "
                    f"({candidate['setup']['type']} {candidate['zone']})")
                signal = analyze_market(candidate['df_1h'], None, pair)

            if signal:
                if config.STRATEGY == 'SMC':
                    # «Почему открылась» для SMC — набор подтверждающих факторов
                    smc_info = signal['smc']
                    signal['scan'] = {
                        'confluence': smc_info['confluence'],
                        'poi_type': smc_info['poi_type'],
                        'factors': [k for k, ok in smc_info['factors'].items() if ok],
                        'sweep': smc_info['sweep'],
                        'rr_first': smc_info['rr_first'],
                        'rr_final': smc_info['rr_final'],
                    }
                    df_for_chart = candidate.get('df_1h')
                else:
                    signal['htf_trend'] = candidate.get('htf_trend', 'NEUTRAL')
                    # Контекст скана — в журнал сделки («почему открылась»)
                    signal['scan'] = {k: candidate.get(k) for k in
                                      ('score', 'score_legacy', 'rr_est', 'htf_strength',
                                       'proximity', 'size_pct', 'funding_bp')}
                    df_for_chart = candidate['df_1h']

                # То же решение счёта, что и в фантомном пути: стороны и
                # деньги. Живой путь собирает сигнал своим кодом, поэтому
                # решение приходится звать дважды: одно на двоих означало бы,
                # что настройка действует в наблюдении и не действует в бою.
                signal, why = account.decide(config.STRATEGY, signal, balance)
                if signal is None:
                    log(f"   {pair}: {why}")
                    continue

                signal['strategy'] = config.STRATEGY
                log(f"СИГНАЛ на {pair}! HTF={signal['htf_trend']}")
                success = trade_manager.execute_trade(signal, df_1h=df_for_chart)
                if success:
                    signals_found += 1
                    open_pairs.add(pair)
            # else: причина отказа уже залогирована внутри analyze_market

        except Exception as e:
            msg = f"Ошибка анализа {pair}: {e}"
            log(msg)
            tg.error_alert(msg)

    log(f"\nИтог цикла: открыто новых сделок — {signals_found}")


def confirm_live_mode():
    log("\n" + "!" * 60)
    log("  ВНИМАНИЕ: ЗАПУСК В LIVE РЕЖИМЕ!")
    log("  БОТ БУДЕТ ТОРГОВАТЬ НА РЕАЛЬНЫЕ ДЕНЬГИ!")
    log("!" * 60)
    log(f"\n  Биржа:        {config.EXCHANGE_NAME.upper()}")
    log(f"  Риск/сделка:  {config.RISK_PER_TRADE}%")
    log(f"  Макс. позиций:{config.MAX_ACTIVE_PAIRS}")
    log(f"  Пул пар:      {len(config.TRADING_PAIRS_POOL)} пар")
    log("")

    # Под службой клавиатуры нет: input() сразу упирается в конец потока, бот
    # падает, петля перезапуска поднимает его снова — и так каждые 15 секунд,
    # молча. Поэтому здесь либо письменное подтверждение в настройках, либо
    # внятный отказ. Требование подтвердить LIVE осознанно этим не смягчается:
    # LIVE_CONFIRMED=YES человек вписывает руками, случайно так не выходит.
    if not sys.stdin or not sys.stdin.isatty():
        if str(getattr(config, 'LIVE_CONFIRMED', '')).strip().upper() == 'YES':
            log("LIVE подтверждён настройкой LIVE_CONFIRMED=YES в .env")
            return
        log("Бот запущен без консоли (служба), подтвердить с клавиатуры некому.")
        log("Чтобы разрешить LIVE службе, впишите в bot_data/.env строку:")
        log("    LIVE_CONFIRMED=YES")
        log("Запуск отменён.")
        sys.exit(1)

    answer = input("Введи 'YES' для подтверждения: ").strip()
    if answer != 'YES':
        log("Запуск отменён.")
        sys.exit(0)

    answer2 = input("Ещё раз введи 'YES' для запуска: ").strip()
    if answer2 != 'YES':
        log("Запуск отменён.")
        sys.exit(0)

    log("Подтверждено. Запускаю LIVE торговлю...")


def _start_paper():
    """
    Поднимает фантомный счёт.

    Ключи API не запрашиваются вообще: рынок читается публичным клиентом, и
    отправить ордер этому коду физически нечем.
    """
    global broker

    if config.PAPER_RESET:
        PaperBroker.archive_previous()

    strategies = (PAPER_STRATEGIES if config.STRATEGY == 'BOTH'
                  else (config.STRATEGY,))
    client = make_market_client(config.EXCHANGE_NAME)
    broker = PaperBroker(client, strategies=strategies,
                         start_balance=config.PAPER_START_BALANCES)

    controller.trade_manager = broker
    controller.start()
    # Сообщения торговых счетов (инструкции пропа, события бирж) — в Telegram.
    trading_accounts.notify_with(tg.account_instruction)

    log("\n👻 ФАНТОМНАЯ ТОРГОВЛЯ — ордера на биржу НЕ отправляются")
    for name in broker.strategies:
        log(f"   {name}: стартовый депозит ${broker.start_balance(name):,.2f} | "
            f"сейчас ${broker.equity(name):,.2f}")
    log(f"   Комиссии: мейкер {config.PAPER_FEE_MAKER * 100:.3f}% / "
        f"тейкер {config.PAPER_FEE_TAKER * 100:.3f}%  |  "
        f"проскальзывание {config.PAPER_SLIPPAGE_PCT * 100:.3f}%")
    log(f"   Фандинг: {'учитывается' if config.PAPER_FUNDING else 'выключен'}")
    # Величины исполнения — у каждой стратегии свои (strategy_profile): в
    # журнале видно, чем живёт заявка, сколько пауза и где предел издержек.
    import strategy_profile
    for name in broker.strategies:
        d = strategy_profile.describe(name)
        log(f"   {name}: заявка {d['expiry_hours']:.0f} ч | кулдаун {d['cooldown_hours']:.0f} ч | "
            f"издержки ≤ {d['cost_limit_pct']:.0f}% риска | смещение лимита {d['limit_offset_pct'] * 100:.2f}% | "
            f"держать ≤ {d['max_hold_hours']:.0f} ч"
            + (" | лимит за рынком — по рынку" if d.get('fills_through_market') else ""))
    # Счета стратегий (accounts/paper.py): чем каждая торгует на тесте.
    for name in broker.strategies:
        a = account.describe(name)
        log(f"   {name}: счёт — {'торгует' if a['enabled'] else 'ВЫКЛЮЧЕНА'} | "
            f"риск {a['risk_pct']:g}% | стороны {a['sides']} | "
            f"позиций {a['max_slots'] or 'без предела'} | "
            f"в одну сторону {a['max_same_direction'] or 'без предела'}")
    if getattr(config, 'PAPER_EXCLUSIVE_PAIRS', True):
        log("   Одна пара — одна позиция на все стратегии, как на бирже "
            "(результаты завышены меньше, сравнение честнее)")
    else:
        log("   Одна пара может быть открыта несколькими стратегиями "
            "(с 24.09.2026: стратегии не отбирают пары друг у друга; "
            "фантомные результаты завышены относительно боя — решение владельца)")
    if getattr(config, 'PORTFOLIO_DAILY_DD_PAUSE_PCT', 0):
        log(f"   Термостат: новые входы стоят при просадке портфеля за день "
            f"≥ {config.PORTFOLIO_DAILY_DD_PAUSE_PCT:.1f}%")
    # Торговые счета (accounts/trading.py): проп по инструкциям и биржи.
    try:
        for line in trading_accounts.describe():
            log(line)
    except Exception as exc:                           # noqa: BLE001
        log(f'⚠️ торговые счета: {exc}')

    dashboard.start_dashboard(broker=broker)
    tg.bot_started(broker.get_real_balance(), broker=broker)



def main():
    global trade_manager, _last_summary_date

    # Сбор ошибок подключается ДО первой строки лога: иначе проблемы старта
    # (нет ключей, недоступна биржа) — самые интересные для разбора — в
    # журнал не попадут.
    error_log.install()

    log("=" * 60)
    mode_label = {'LIVE': 'LIVE', 'PAPER': 'ФАНТОМ'}.get(config.TRADING_MODE, 'DEMO')
    log(f"KRAKEN — {mode_label}")
    log("=" * 60)

    log(f"\nСтратегия:     {config.STRATEGY}")
    log(f"Пул пар:       {len(config.TRADING_PAIRS_POOL)} пар")
    log(f"Макс. позиций: {config.MAX_ACTIVE_PAIRS}")
    log(f"Риск/сделка:   {config.RISK_PER_TRADE}%")
    log(f"Мин. объём:    ${config.MIN_VOLUME_24H_USD/1e6:.0f}M / 24ч")

    if config.STRATEGY == 'SMC':
        from smc import params as smc_params
        log(f"Таймфреймы:    bias {smc_params.TF_BIAS} -> "
            f"HTF {smc_params.TF_HTF} -> зоны {smc_params.TF_POI}")
        log(f"Типы зон:      {', '.join(smc_params.POI_TYPES_ENABLED) or 'все'}")
        log(f"Confluence:    >= {smc_params.MIN_CONFLUENCE_SCORE}")
        log(f"Мин. RR:       {smc_params.MIN_RR}")
        log(f"Стоп:          {smc_params.SL_MODE}")
    else:
        log(f"Мин. импульс:  {config.MIN_IMPULSE_PCT}%")
        log(f"HTF таймфрейм: {config.HTF_TIMEFRAME}  "
            f"EMA {config.HTF_EMA_FAST}/{config.HTF_EMA_SLOW}")
        log(f"Трейлинг:      после TP{config.TRAIL_AFTER_TP}")
    log(f"Кулдаун:       {config.COOLDOWN_HOURS} ч (общий; у стратегий — свой, см. ниже)")

    if config.PAPER_MODE:
        _start_paper()
        _last_summary_date = date.today()
        _run_scheduler()
        return

    if config.TRADING_MODE == 'LIVE':
        confirm_live_mode()

    trade_manager = LiveTradeManager()
    controller.trade_manager = trade_manager
    controller.start()

    if not trade_manager.test_connection():
        log("Не удалось подключиться к бирже. Завершение.")
        tg.error_alert("Не удалось подключиться к бирже при старте!")
        sys.exit(1)

    balance = trade_manager.get_real_balance()
    log(f"\nРеальный баланс: ${balance:.2f}")

    # Дашборд поднимается после подключения к бирже, чтобы сразу показывать
    # актуальный баланс. Сбой запуска торговлю не прерывает.
    dashboard.start_dashboard(trade_manager=trade_manager)

    # Первый daily summary — на текущую дату (чтобы не слать пустой)
    _last_summary_date = date.today()

    tg.bot_started(balance)
    _run_scheduler()


def _run_scheduler():
    """Основной цикл: один и тот же для боевого и фантомного режимов."""
    executor = trade_manager if trade_manager is not None else broker

    scheduler = BlockingScheduler()
    scheduler.add_job(
        trading_cycle, 'interval', minutes=5, next_run_time=datetime.now()
    )

    log("\nБот запущен!")
    log(f"Сканирую {len(config.TRADING_PAIRS_POOL)} пар каждые 5 минут...")
    log("Нажми Ctrl+C для остановки\n")

    # Состояние для дашборда. До этой строки его сообщало ТОЛЬКО настольное
    # приложение, поэтому запущенный службой бот честно торговал, а индикатор
    # внизу слева навсегда оставался «запуск…»: понять по нему, жив бот или
    # нет, было нельзя — а именно за этим на него и смотрят.
    dashboard.set_status('running')

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log("\n\nБот остановлен пользователем")
        dashboard.set_status('stopped', 'остановлен с клавиатуры')
        controller.stop()
        history     = executor.trade_history
        session_pnl = sum(t.get('pnl', 0) for t in history)
        tg.bot_stopped(len(history), session_pnl, executor.get_real_balance())
        if trade_manager is not None:
            trade_manager.get_stats()
    except Exception as exc:                      # noqa: BLE001
        # Упавший планировщик — это конец торговли, и узнать об этом надо с
        # дашборда, а не по тому, что сделок давно нет. Ошибка не глотается:
        # она поднимается дальше, и петля перезапуска увидит ненулевой код.
        dashboard.set_status('error', str(exc)[:200])
        log(f"\n❌ Цикл торговли остановлен ошибкой: {exc}")
        raise


if __name__ == "__main__":
    main()
