import sys
from datetime import datetime, date, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler

import config
import telegram_notify as tg
from telegram_bot import controller
from exchange import get_exchange, fetch_ohlcv
from strategy import analyze_market
from pair_scanner import get_liquid_pairs, scan_for_setups
from trade_manager import LiveTradeManager
from logger import log

trade_manager   = None
_last_summary_date = None   # tracks date of last daily summary sent


def _send_daily_summary_if_needed():
    """Sends daily summary once per day at the first cycle after midnight."""
    global _last_summary_date
    today = date.today()
    if _last_summary_date == today:
        return
    _last_summary_date = today

    if trade_manager is None:
        return

    # Collect yesterday's closed trades from trade_history
    history      = trade_manager.trade_history
    yesterday    = today - timedelta(days=1)
    today_trades = [t for t in history
                    if t.get('exit_time') and t['exit_time'].date() == yesterday]
    total    = len(today_trades)
    wins     = sum(1 for t in today_trades if t.get('pnl', 0) > 0)
    daily_pnl = sum(t.get('pnl', 0) for t in today_trades)
    balance  = trade_manager.get_real_balance()
    tg.daily_summary(total, wins, daily_pnl, balance)


def trading_cycle():
    global _last_summary_date

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

    if available <= 0:
        log("Все слоты заняты — новые входы пропускаем")
        return

    # ── Сессионный фильтр: в блок-часы UTC новые сетапы не открываем ──────
    # (позиции и pending уже обслужены выше — блокируется только скан/вход)
    cur_hour_utc = datetime.now(timezone.utc).hour
    if cur_hour_utc in config.BLOCK_ENTRY_HOURS_UTC:
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

        log(f"\nПроверяю вход в зону A: {pair} ({candidate['setup']['type']} {candidate['zone']})")

        try:
            signal = analyze_market(candidate['df_1h'], None, pair, balance)

            if signal:
                signal['htf_trend'] = candidate.get('htf_trend', 'NEUTRAL')
                # Контекст скана — в журнал сделки («почему открылась»)
                signal['scan'] = {k: candidate.get(k) for k in
                                  ('score', 'score_legacy', 'rr_est', 'htf_strength',
                                   'proximity', 'size_pct')}
                log(f"СИГНАЛ на {pair}! HTF={signal['htf_trend']}")
                success = trade_manager.execute_trade(signal, df_1h=candidate['df_1h'])
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


def main():
    global trade_manager, _last_summary_date

    log("=" * 60)
    mode_label = "LIVE" if config.TRADING_MODE == 'LIVE' else "DEMO"
    log(f"KRAKEN — {mode_label}")
    log("=" * 60)

    log(f"\nПул пар:       {len(config.TRADING_PAIRS_POOL)} пар")
    log(f"Макс. позиций: {config.MAX_ACTIVE_PAIRS}")
    log(f"Риск/сделка:   {config.RISK_PER_TRADE}%")
    log(f"Мин. объём:    ${config.MIN_VOLUME_24H_USD/1e6:.0f}M / 24ч")
    log(f"Мин. импульс:  {config.MIN_IMPULSE_PCT}%")
    log(f"HTF таймфрейм: {config.HTF_TIMEFRAME}  EMA {config.HTF_EMA_FAST}/{config.HTF_EMA_SLOW}")
    log(f"Трейлинг:      после TP{config.TRAIL_AFTER_TP}")
    log(f"Кулдаун:       {config.COOLDOWN_HOURS} ч")

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

    # Первый daily summary — на текущую дату (чтобы не слать пустой)
    _last_summary_date = date.today()

    tg.bot_started(balance)

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
        history     = trade_manager.trade_history
        session_pnl = sum(t.get('pnl', 0) for t in history)
        tg.bot_stopped(len(history), session_pnl, trade_manager.get_real_balance())
        trade_manager.get_stats()


if __name__ == "__main__":
    main()
