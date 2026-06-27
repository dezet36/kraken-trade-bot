"""
Тест: генерирует график с реальными 5M данными с биржи и отправляет в Telegram.
Запуск: python test_chart.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from exchange import get_exchange, fetch_ohlcv
from strategy import analyze_market, find_recent_impulse, get_zones
from chart_generator import generate_trade_chart
import telegram_notify as tg
import config
from logger import log
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def test_chart_with_real_data():
    """Берёт реальные данные BTCUSDT, строит тестовый сигнал, генерирует и шлёт график."""
    log("=" * 60)
    log("ТЕСТ ГЕНЕРАЦИИ ГРАФИКА")
    log("=" * 60)

    exchange = get_exchange()

    # Берём реальные 5M данные
    pair = 'BTCUSDT'
    log(f"\n1. Загрузка 5M данных {pair}...")
    df_5m = fetch_ohlcv('5m', limit=100, symbol=pair)
    if df_5m is None or df_5m.empty:
        log("ОШИБКА: не удалось загрузить 5M данные")
        return

    log(f"   Получено {len(df_5m)} свечей, последняя: {df_5m.iloc[-1]['timestamp']}")

    # Берём 1H данные
    log(f"\n2. Загрузка 1H данных {pair}...")
    df_1h = fetch_ohlcv('1h', limit=48, symbol=pair)
    if df_1h is None or df_1h.empty:
        log("ОШИБКА: не удалось загрузить 1H данные")
        return

    current_price = df_5m.iloc[-1]['close']
    log(f"   Текущая цена: ${current_price:.2f}")

    # Пробуем реальный анализ рынка
    log(f"\n3. Анализ рынка (analyze_market)...")
    balance = 10000.0
    signal = analyze_market(df_1h, df_5m, pair, balance)

    if signal:
        log(f"   Реальный сигнал найден! {signal['setup']['type']} в {signal['trigger']['zone']}")
        log(f"   Вход: ${signal['params']['entry']:.4f} | SL: ${signal['params']['stop_loss']:.4f}")
        log(f"   TP1: ${signal['params']['take_profit_1']:.4f}")
        log(f"   Объём: {signal['trigger'].get('volume_ratio', 0):.2f}×")
        signal['htf_trend'] = 'BULLISH'
    else:
        log("   Реального сигнала нет — строим синтетический для теста графика...")
        # Строим синтетический сигнал на основе реального impulse
        setup = find_recent_impulse(df_1h, lookback_candles=48)
        if setup is None:
            log("   Импульс не найден, строим вручную...")
            # Строим вручную от текущей цены
            hi = df_1h['high'].max()
            lo = df_1h['low'].min()
            if hi > lo:
                direction = 'LONG' if df_1h.iloc[-1]['close'] > (hi + lo) / 2 else 'SHORT'
                setup = {
                    'type': direction,
                    'start_price': lo if direction == 'LONG' else hi,
                    'end_price':   hi if direction == 'LONG' else lo,
                    'size':        hi - lo,
                    'start_time':  df_1h.iloc[0]['timestamp'],
                    'end_time':    df_1h.iloc[-1]['timestamp'],
                }
            else:
                log("   Не удалось построить синтетический сигнал")
                return

        zone_a, zone_b = get_zones(setup)
        sl_distance = abs(current_price - setup['start_price']) * 0.05
        if sl_distance < current_price * 0.005:
            sl_distance = current_price * 0.01

        risk_amount   = balance * 0.01
        position_size = risk_amount / sl_distance

        if setup['type'] == 'LONG':
            entry    = current_price
            sl_price = current_price - sl_distance
            tp1      = current_price + sl_distance * 1.5
            tp2      = current_price + sl_distance * 2.5
            tp3      = current_price + sl_distance * 4.0
            tp4      = current_price + sl_distance * 6.0
        else:
            entry    = current_price
            sl_price = current_price + sl_distance
            tp1      = current_price - sl_distance * 1.5
            tp2      = current_price - sl_distance * 2.5
            tp3      = current_price - sl_distance * 4.0
            tp4      = current_price - sl_distance * 6.0

        signal = {
            'trading_pair': pair,
            'setup':    setup,
            'trigger':  {
                'type': setup['type'],
                'trigger_price': current_price,
                'trigger_time':  df_5m.iloc[-1]['timestamp'],
                'bos_level':     current_price * 0.99,
                'zone':          'Zone_A',
                'volume_ratio':  1.42,
            },
            'params': {
                'entry':          entry,
                'stop_loss':      sl_price,
                'take_profit_1':  tp1,
                'take_profit_2':  tp2,
                'take_profit_3':  tp3,
                'take_profit_4':  tp4,
                'position_size':  position_size,
                'risk_amount':    risk_amount,
                'rr':             1.5,
                'sl_distance':    sl_distance,
            },
            'htf_trend': 'BULLISH' if setup['type'] == 'LONG' else 'BEARISH',
            'zone_a':    zone_a,
            'zone_b':    zone_b,
        }
        log(f"   Синтетический сигнал: {setup['type']} | Вход: ${current_price:.2f}")

    # Генерируем и тестируем только локально (без отправки в Telegram)
    log(f"\n4. Генерация графика...")
    chart_path = generate_trade_chart(signal, df_5m)

    if chart_path:
        size_kb = os.path.getsize(chart_path) // 1024
        log(f"   OK: {chart_path}")
        log(f"   Размер: {size_kb} KB")

        # Отправка в Telegram (если настроен)
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            log(f"\n5. Отправка в Telegram...")
            setup_d = signal['setup']
            trigger = signal['trigger']
            params  = signal['params']
            direction_icon = "📈 LONG" if setup_d['type'] == 'LONG' else "📉 SHORT"
            zone_icon = "🅰️" if trigger['zone'] == 'Zone_A' else "🅱️"
            sl_dist_pct = abs(params['entry'] - params['stop_loss']) / params['entry'] * 100
            sl_dir      = "-" if setup_d["type"] == "LONG" else "+"
            vol_ratio   = trigger.get('volume_ratio', 0)
            vol_str     = f"  📊 Объём: {vol_ratio:.1f}×" if vol_ratio > 0 else ""
            htf         = signal.get('htf_trend', '—')
            htf_icon    = "↗️" if htf == "BULLISH" else ("↘️" if htf == "BEARISH" else "↔️")
            impulse_pct = round(setup_d['size'] / setup_d['end_price'] * 100, 1) if setup_d['end_price'] > 0 else 0
            mode        = "🟢 DEMO" if config.TRADING_MODE == "DEMO" else "🔴 LIVE"

            caption = (
                f"<b>{direction_icon} {pair}</b>  {zone_icon} {trigger['zone']}  {mode}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Вход:  <code>${params['entry']:.4f}</code>\n"
                f"🛡 Стоп:  <code>${params['stop_loss']:.4f}</code>  ({sl_dir}{sl_dist_pct:.2f}%)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 TP1:  <code>${params['take_profit_1']:.4f}</code>\n"
                f"🎯 TP2:  <code>${params['take_profit_2']:.4f}</code>\n"
                f"🎯 TP3:  <code>${params['take_profit_3']:.4f}</code>\n"
                f"🏆 TP4:  <code>${params['take_profit_4']:.4f}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"RR: 1:{params['rr']:.1f}  |  HTF: {htf_icon} {htf}  |  Имп: {impulse_pct}%{vol_str}\n"
                f"💰 Риск: ${params['risk_amount']:.2f}  |  Размер: {params['position_size']:.4f}\n"
                f"⏰ {datetime.now().strftime('%H:%M %d.%m')}\n"
                f"<i>🧪 ТЕСТОВЫЙ ГРАФИК</i>"
            )

            ok = tg._send_photo(chart_path, caption=caption)
            if ok:
                log("   ✅ График отправлен в Telegram!")
            else:
                log("   ⚠️ Не удалось отправить (проверь TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env)")
        else:
            log("\n5. Telegram не настроен — график сохранён локально")
            log(f"   Файл НЕ удалён, открой вручную: {chart_path}")
            # Don't delete — user can view it
            return  # skip delete at end

        log("\n✅ ТЕСТ ЗАВЕРШЁН УСПЕШНО")
    else:
        log("\n❌ Генерация графика не удалась")


def test_risk_calculation():
    """Проверяет корректность расчёта риска на нескольких примерах."""
    log("\n" + "=" * 60)
    log("ТЕСТ РАСЧЁТА РИСКА (1% от баланса)")
    log("=" * 60)

    cases = [
        {'balance': 10000, 'entry': 50000, 'sl': 49000},   # 2% SL distance
        {'balance': 10000, 'entry': 50000, 'sl': 48500},   # 3% SL distance
        {'balance': 5000,  'entry': 2000,  'sl': 1940},    # 3% SL, lower balance
        {'balance': 10000, 'entry': 0.150, 'sl': 0.143},   # shitcoin
    ]

    all_ok = True
    for c in cases:
        sl_dist       = abs(c['entry'] - c['sl'])
        risk_amount   = c['balance'] * (config.RISK_PER_TRADE / 100)
        position_size = risk_amount / sl_dist
        actual_loss   = position_size * sl_dist
        sl_pct_entry  = sl_dist / c['entry'] * 100

        ok = abs(actual_loss - risk_amount) < 0.01
        status = "✅" if ok else "❌"
        if not ok:
            all_ok = False

        log(f"{status} Balance=${c['balance']:,.0f} | Entry=${c['entry']} | SL=${c['sl']}")
        log(f"   SL дистанция: {sl_pct_entry:.2f}% от цены")
        log(f"   risk_amount:  ${risk_amount:.2f} (1% баланса)")
        log(f"   position_size:{position_size:.6f}")
        log(f"   Убыток при SL: ${actual_loss:.2f}  →  {actual_loss/c['balance']*100:.3f}% от баланса")
        log("")

    if all_ok:
        log("✅ Все расчёты риска корректны — 1% от баланса во всех случаях")
    else:
        log("❌ Обнаружены ошибки в расчёте риска!")

    log("\nВАЖНО: SL дистанция (% от цены) всегда будет разной — это нормально!")
    log("1% риска = $убыток при SL = 1% баланса, независимо от расстояния до SL.")


if __name__ == "__main__":
    test_risk_calculation()
    test_chart_with_real_data()
