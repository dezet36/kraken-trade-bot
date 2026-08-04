"""
SMC — ядро стратегии Smart Money Concepts.

Пакет намеренно лежит ВНУТРИ Live_Bot/: по модели деплоя папка Live_Bot
перезаписывается целиком, и ядро снаружи неё потерялось бы при обновлении.

Пакет не импортирует config.py, exchange.py и logger.py: он чистый и
считает только по DataFrame свечей. Благодаря этому один и тот же код
используется и живым ботом, и бэктестом (research/), и юнит-тестами —
без дублирования логики, как это было у фибо-стратегии.

Слои (снизу вверх):
    params      — все настройки SMC
    swings      — фрактальные свинги (раздел 2.1 методички)
    structure   — HH/HL/LH/LL, BOS/mBOS, SMS (раздел 2.2-2.5)
    liquidity   — BSL/SSL, EQH/EQL, PDH/PDL, снятия (раздел 3)
    imbalance   — FVG / имбаланс (раздел 4)
    poi         — Order Block, Breaker, Mitigation, Wick (раздел 5)
    fib         — premium/discount, OTE, цели (раздел 10, 22)
    sessions    — killzones, открытия, азиатский рендж (раздел 11)
    signal      — сборка торгового сетапа из всего вышеперечисленного
"""

__all__ = [
    'params', 'swings', 'structure', 'liquidity',
    'imbalance', 'poi', 'fib', 'sessions', 'signal',
]
