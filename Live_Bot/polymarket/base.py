"""
Общий контракт сигнальных модулей: раздел 4.1 спецификации.

ЗАЧЕМ ЕДИНЫЙ ИНТЕРФЕЙС. Стратегий три, и они разные по природе: одна считает
вероятность из чужого рынка (опционы, ставки), вторая — из статистического
искажения, третья — из физической модели. Общее у них ровно одно: каждая
отвечает числом «наша вероятность» на вопрос конкретного рынка. Всё остальное —
размер ставки, лимиты, запись решения — обязано быть общим, иначе три копии
риск-менеджмента разойдутся, и разойдутся молча.

CONFIDENCE — ОБЯЗАТЕЛЬНОЕ ПОЛЕ, А НЕ УКРАШЕНИЕ. Не всякое расхождение одинаково
надёжно: прогноз погоды на станции с разбросом 0.5 градуса и прогноз на станции
с разбросом 2 градуса дают одинаковое «расхождение», но верить им надо
по-разному. Слой риска умножает расхождение на уверенность ДО сравнения с
порогом; без этого редкий уверенный сигнал утонул бы среди частых сомнительных.

ВАЛИДАЦИЯ ХРАНИТСЯ ПРИ СТРАТЕГИИ И ЧИТАЕТСЯ КОДОМ. В этом проекте не бывает
«вроде проверяли»: у каждой стратегии стоит статус, дата и причина. Стратегия с
непройденной валидацией не может выйти на реальные деньги — это проверяется, а
не подразумевается.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


PASSED = 'ПРОЙДЕНА'
FAILED = 'ПРОВАЛЕНА'
UNTESTED = 'НЕ ПРОВОДИЛАСЬ'


@dataclass(frozen=True)
class Validation:
    """
    Состояние проверки стратегии на исторических данных.

    `note` обязана объяснять, ПОЧЕМУ такой статус, а не повторять его словами:
    через полгода помнить причину будет некому.
    """
    status: str
    note: str
    checked_at: str = ''

    @property
    def allows_real_money(self):
        return self.status == PASSED


@dataclass
class SignalResult:
    """Одно расхождение: наша вероятность против цены рынка."""
    model_probability: float
    market_probability: float
    confidence: float
    data_sources: list
    market: dict = field(default_factory=dict)
    cost: float = 0.0
    liquidity: float = 0.0
    note: str = ''
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
                                        .strftime('%Y-%m-%dT%H:%M:%SZ'))

    @property
    def edge(self):
        return self.model_probability - self.market_probability

    def as_risk_input(self, strategy):
        """
        Перевод в словарь, который понимает слой риска.

        Отдельный метод, а не общий словарь на входе, потому что слой риска
        обязан получать ОДИН формат от всех трёх стратегий: разные ключи у
        разных модулей — самый тихий способ получить разные лимиты там, где
        задуманы одинаковые.
        """
        market = self.market or {}
        return {
            'strategy': strategy,
            'price': self.market_probability,
            'model': self.model_probability,
            'confidence': self.confidence,
            'cost': self.cost,
            'liquidity': self.liquidity,
            'market': market,
            'event': ((market.get('events') or [{}])[0]).get('id') or market.get('id'),
            'category': market.get('feeType'),
        }


class SignalModule(ABC):
    """Один модуль на стратегию. Имя и валидация обязательны."""

    name = 'без имени'
    validation = Validation(UNTESTED, 'проверка не проводилась')

    @abstractmethod
    def scan(self, markets):
        """Список SignalResult по переданным рынкам. Непонятные — пропускать."""

    def allows_real_money(self):
        """
        Разрешение на живые деньги. Читается исполнителем ПЕРЕД отправкой.

        Метод, а не поле, чтобы модуль мог добавить своё условие сверх
        валидации; переопределяя его, нельзя случайно ослабить общее правило —
        проверка статуса остаётся здесь.
        """
        return self.validation.allows_real_money
