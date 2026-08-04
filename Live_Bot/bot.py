import sys
from datetime import datetime, date, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler

import config
import telegram_notify as tg
from telegram_bot import controller
from exchange import get_exchange, make_market_client, fetch_ohlcv
import dashboard
import strategy_smc
from strategy import analyze_market
from pair_scanner import get_liquid_pairs, scan_for_setups
from paper_broker import PaperBroker, STRATEGIES as PAPER_STRATEGIES
import settings_store as settings
from trade_manager import LiveTradeManager
from logger import log

trade_manager   = None
broker          = None      # фантомный счёт (TRADING_MODE=PAPER)
_last_summary_date = None   # tracks date of last daily summary sent


def _send_daily_summary_if_needed():
    """Sends daily summary once per day at the first cycle after midnight."""
    global _last_summary_date
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
    total    = len(today_trades)
    wins     = sum(1 for t in today_trades if t.get('pnl', 0) > 0)
    daily_pnl = sum(t.get('pnl', 0) for t in today_trades)
    balance  = executor.get_real_balance()
    tg.daily_summary(total, wins, daily_pnl, balance)


def _build_signal(candidate, strategy, balance):
    """
    Достраивает из кандидата сканера готовый сигнал.

    Возвращает (signal, df_1h) либо (None, None). Контекст «почему открылась»
    кладётся в signal['scan'] — он же уходит в журнал и в дашборд, поэтому по
    закрытой сделке потом видно, на каком основании в неё зашли.
    """
    pair = candidate['pair']

    if strategy == 'SMC':
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
        df_for_chart = candidate.get('df_1h')
        log(f"\n[SMC] {pair}: зона {candidate['poi_type']}, "
            f"confluence {candidate['score']}, RR {candidate['rr']:.2f}")
    else:
        signal = analyze_market(candidate['df_1h'], None, pair, balance)
        if not signal:
            return None, None
        signal['htf_trend'] = candidate.get('htf_trend', 'NEUTRAL')
        signal['scan'] = {k: candidate.get(k) for k in
                          ('score', 'score_legacy', 'rr_est', 'htf_strength',
                           'proximity', 'size_pct')}
        df_for_chart = candidate['df_1h']
        log(f"\n[FIBO] {pair}: зона {candidate.get('zone')}, "
            f"HTF {signal['htf_trend']}")

    signal['strategy'] = strategy
    return signal, df_for_chart


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

    scanners = (
        ('FIBO', lambda: scan_for_setups(liquid_pairs, trade_manager)),
        ('SMC', lambda: strategy_smc.scan_for_setups(liquid_pairs, trade_manager,
                                                     balance=balance)),
    )

    for strategy, scan in scanners:
        if not settings.enabled(strategy):
            log(f"\n=== {strategy}: выключена оператором, пропускаем ===")
            continue

        budget = settings.max_slots(strategy)
        used = trade_manager.slots_used_by(strategy)
        free = budget - used
        log(f"\n=== {strategy}: занято {used}/{budget} слотов ===")
        if free <= 0:
            log(f"   {strategy}: слоты заняты, пропускаем")
            continue

        # Сессионный фильтр откалиброван под фибо; у SMC своя модель времени
        if strategy == 'FIBO':
            hour = datetime.now(timezone.utc).hour
            if hour in config.BLOCK_ENTRY_HOURS_UTC:
                log(f"   FIBO: {hour:02d}:xx UTC в блок-листе, пропускаем")
                continue

        try:
            candidates = scan()
        except Exception as exc:
            log(f"   {strategy}: ошибка сканирования — {exc}")
            tg.error_alert(f"{strategy}: ошибка сканирования — {exc}")
            continue

        log(f"   {strategy}: сетапов найдено {len(candidates)}")
        opened = 0
        for candidate in candidates:
            if opened >= free:
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

    if controller.is_paused():
        log("⏸ Бот на паузе — новые фантомные входы пропускаем")
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

    hour_utc = datetime.now(timezone.utc).hour
    total_opened = 0

    for strategy in broker.strategies:
        if not settings.enabled(strategy):
            log(f"\n=== {strategy}: выключена оператором, пропускаем ===")
            continue

        balance = broker.balance(strategy)
        used = broker.slots_used_by(strategy)
        budget = settings.max_slots(strategy)
        free = budget - used
        equity = broker.equity(strategy)
        start = broker.start_balance(strategy)
        growth = (equity / start - 1) * 100 if start else 0.0

        log(f"\n=== {strategy}: депозит ${equity:,.2f} ({growth:+.2f}%) | "
            f"слотов {used}/{budget} ===")
        if free <= 0:
            log(f"   {strategy}: слоты заняты, пропускаем")
            continue
        if balance <= 0:
            log(f"   {strategy}: депозит обнулён, торговля остановлена")
            continue

        # Сессионный фильтр откалиброван под фибо; у SMC своя модель времени
        if strategy == 'FIBO' and hour_utc in config.BLOCK_ENTRY_HOURS_UTC:
            log(f"   FIBO: {hour_utc:02d}:xx UTC в блок-листе, пропускаем")
            continue

        gate = broker.gate(strategy)
        try:
            if strategy == 'SMC':
                candidates = strategy_smc.scan_for_setups(
                    liquid_pairs, gate, client=client, balance=balance)
            else:
                candidates = scan_for_setups(liquid_pairs, gate, client=client)
        except Exception as exc:
            log(f"   {strategy}: ошибка сканирования — {exc}")
            continue

        log(f"   {strategy}: сетапов найдено {len(candidates)}")
        opened = 0
        for candidate in candidates:
            if opened >= free:
                break
            try:
                signal, _ = _build_signal(candidate, strategy, balance)
                if signal and broker.open(strategy, signal):
                    opened += 1
                    total_opened += 1
            except Exception as exc:
                log(f"   {strategy}: ошибка входа {candidate['pair']} — {exc}")

        log(f"   {strategy}: поставлено ордеров — {opened}")

    log(f"\nИтог цикла: новых фантомных ордеров — {total_opened}")


def trading_cycle():
    global _last_summary_date

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

    if not settings.enabled(config.STRATEGY):
        log(f"{config.STRATEGY}: выключена оператором — новые входы пропускаем")
        return

    if config.STRATEGY == 'SMC':
        log("\nПоиск SMC-сетапов (bias 1D/4H -> зоны 1H)...")
        candidates = strategy_smc.scan_for_setups(liquid_pairs, trade_manager,
                                                  balance=balance)
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
                signal = analyze_market(candidate['df_1h'], None, pair, balance)

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
                                       'proximity', 'size_pct')}
                    df_for_chart = candidate['df_1h']

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

    log("\n👻 ФАНТОМНАЯ ТОРГОВЛЯ — ордера на биржу НЕ отправляются")
    for name in broker.strategies:
        log(f"   {name}: стартовый депозит ${broker.start_balance(name):,.2f} | "
            f"сейчас ${broker.equity(name):,.2f}")
    log(f"   Комиссии: мейкер {config.PAPER_FEE_MAKER * 100:.3f}% / "
        f"тейкер {config.PAPER_FEE_TAKER * 100:.3f}%  |  "
        f"проскальзывание {config.PAPER_SLIPPAGE_PCT * 100:.3f}%")
    log(f"   Фандинг: {'учитывается' if config.PAPER_FUNDING else 'выключен'}")
    log("   Одна пара может быть открыта обеими стратегиями одновременно")

    dashboard.start_dashboard(broker=broker)
    tg.bot_started(broker.get_real_balance())


def main():
    global trade_manager, _last_summary_date

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
    log(f"Кулдаун:       {config.COOLDOWN_HOURS} ч")

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

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log("\n\nБот остановлен пользователем")
        controller.stop()
        history     = executor.trade_history
        session_pnl = sum(t.get('pnl', 0) for t in history)
        tg.bot_stopped(len(history), session_pnl, executor.get_real_balance())
        if trade_manager is not None:
            trade_manager.get_stats()


if __name__ == "__main__":
    main()
