"""
Контекст рынка с правилами решений SMC по умолчанию (этап 10).

Сам контекст — структура рынка и движок правил — общий слой
analysis/smc/context.py: правил решений у него нет, их передаёт стратегия.
Здесь — контекст, который по умолчанию решает правилами SMC
(strategies/smc/params). Им пользуются замеры research/ и проверки: прежний
вызов ctx.evaluate(i) решает так же, как до переезда. Живой бот берёт
контекст из общего слоя (market_structure) и передаёт правила SMC явно.
"""

from analysis.smc import context as _context
from analysis.smc.context import (BEARISH, BULLISH, NEUTRAL, align_index,  # noqa: F401
                                  bar_duration_ns, to_ns)

from . import params


class MarketContext(_context.MarketContext):
    def evaluate(self, at_index, decision=None):
        return super().evaluate(at_index, params if decision is None else decision)


def build_context(frames, pair=None):
    """Фабрика контекста с правилами SMC — точка входа замеров и проверок."""
    return MarketContext(frames, pair=pair)
