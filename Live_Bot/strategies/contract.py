"""
Договор «сетап» (Стратегии → Счета): что стратегия отдаёт и чего НЕ отдаёт.

Сигнал стратегии — словарь, который читают счёт, исполнители, журналы,
Telegram и панель:

    trading_pair, strategy
    setup     {type: LONG|SHORT, start_price, end_price, size, start_time, ...}
    trigger   {zone, entry_type: LIMIT|MARKET|ZONE_LIMIT|STOP, trigger_price}
    params    {entry, stop_loss, take_profit_1, take_profit_2, tp_targets,
               tp_fractions, be_level, breakeven_after_tp, rr, sl_distance,
               необязательные: max_hold_hours, invalidation, cancel_beyond,
               risk_scale}
    htf_trend, zone, score, why, market_price и свой раздел стратегии
    (smc, levels, rsibb, smcs, fib12, llm).

БЕЗ ДЕНЕГ (реорганизация, этап 2, 08.10.2026). Риск на сделку, размер позиции
и предел позиций в одну сторону решает СЧЁТ (accounts/paper.py) и вписывает
их в params сам — поля MONEY_KEYS. Стратегия не знает ни депозита, ни
процента риска: на тесте риск у всех один (CLAUDE.md, «Риск»), а на реальном
счёте его задаёт владелец счёта.

Множитель риска сетапа `risk_scale` — часть сетапа, а не деньги: так стратегия
измерена (SMC урезает сделку вдвое в трендовом режиме BTC, smc/regime.py). Счёт
умножает на него свой риск; нет поля — множитель 1.
"""

# Поля params, которые пишет счёт. Стратегия их не кладёт.
MONEY_KEYS = ('risk_pct', 'risk_amount', 'position_size', 'max_same_direction')


def money_in(signal):
    """Денежные поля, которые стратегия положила в сигнал (должно быть пусто)."""
    params = (signal or {}).get('params') or {}
    return sorted(key for key in MONEY_KEYS if key in params)
