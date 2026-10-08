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
               pending_invalidation (уровень снятия ждущей заявки; нет —
               стоп)}
    htf_trend, zone, score, why, market_price и свой раздел стратегии
    (smc, levels, rsibb, smcs, fib12, llm).

БЕЗ ДЕНЕГ (реорганизация, этап 2, 08.10.2026). Риск на сделку, размер позиции
и предел позиций в одну сторону решает СЧЁТ (accounts/paper.py) и вписывает
их в params сам — поля MONEY_KEYS. Стратегия не знает ни депозита, ни
процента риска: на тесте риск у всех один (CLAUDE.md, «Риск»), а на реальном
счёте его задаёт владелец счёта.

Множителей риска у сетапа нет: риск у всех стратегий один (решение владельца
08.10.2026 — и SMC в тренде BTC торгует тем же 1%, а не половиной, как до
этого).
"""

# Поля params, которые пишет счёт. Стратегия их не кладёт.
MONEY_KEYS = ('risk_pct', 'risk_amount', 'position_size', 'max_same_direction')


def money_in(signal):
    """Денежные поля, которые стратегия положила в сигнал (должно быть пусто)."""
    params = (signal or {}).get('params') or {}
    return sorted(key for key in MONEY_KEYS if key in params)
