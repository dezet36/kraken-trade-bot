"""
Размер ставки и лимиты. Слой, на котором ломается большинство ботов.

ГЛАВНОЕ ЗДЕСЬ — НЕ ФОРМУЛА, А ПОТОЛКИ. Критерий Келли оптимален лишь тогда,
когда вероятность известна ТОЧНО. Наша оценена, и ошибка в ней при полном Келли
ведёт не к меньшей прибыли, а к разорению: формула отвечает на вопрос «сколько
ставить, если я прав», а не «сколько ставить, если я могу ошибаться». Поэтому
берётся четверть Келли И сверху накладываются жёсткие потолки, которые
срабатывают раньше формулы.

ПОТОЛОК НА СОБЫТИЕ ВАЖНЕЕ ПОТОЛКА НА КОРЗИНУ. Одиннадцать корзин одного города
и дня — это ОДИН исход погоды. Поставив по потолку в каждую, мы получили бы
одиннадцатикратную концентрацию, называя её диверсификацией. Ровно эта ошибка
описана в спецификации как «скрытая концентрация риска», и она же испортила бы
любой замер, считающий корзины независимыми.
"""

from dataclasses import dataclass, field

from . import params


@dataclass
class Decision:
    action: str                  # 'SKIP' | 'PROPOSE'
    size_usd: float = 0.0
    reason: str = ''
    kelly: float = 0.0
    details: dict = field(default_factory=dict)


def kelly_fraction(probability, price):
    """
    Доля банкролла по Келли для бинарной ставки.

    Ставка p приносит 1 при выигрыше, значит коэффициент b = (1 - p) / p.
    Отрицательный результат означает, что ставка невыгодна вовсе.
    """
    if not 0 < price < 1:
        return 0.0
    b = (1 - price) / price
    q = 1 - probability
    edge = (b * probability - q) / b
    return max(edge, 0.0)


class RiskManager:
    """
    Решение по одной ставке с учётом уже открытых.

    Открытые позиции передаются списком словарей с ключами: event, category,
    size_usd. Хранение — не забота этого слоя; он считает и отвечает.
    """

    def __init__(self, bankroll=None, day_loss_usd=0.0):
        self.bankroll = float(bankroll or params.BANKROLL)
        self.day_loss_usd = float(day_loss_usd)

    def evaluate(self, signal, open_positions=None):
        """signal — словарь из weather.signals либо любой с теми же полями."""
        positions = list(open_positions or [])
        price = float(signal.get('price') or 0)
        model = float(signal.get('model') or 0)
        edge = model - price
        cost = float(signal.get('cost') or 0)
        confidence = float(signal.get('confidence', 1.0))

        # Аварийная остановка идёт ПЕРВОЙ: после неё ничего не считается.
        limit = self.bankroll * params.DAILY_LOSS_STOP_PCT / 100
        if self.day_loss_usd >= limit:
            return Decision('SKIP', reason=f'дневной убыток {self.day_loss_usd:.2f} '
                                           f'достиг предела {limit:.2f}')

        if not params.MIN_PRICE <= price <= params.MAX_PRICE:
            return Decision('SKIP', reason=f'цена {price:.3f} вне диапазона '
                                           f'{params.MIN_PRICE}-{params.MAX_PRICE}')
        if float(signal.get('liquidity') or 0) < params.MIN_LIQUIDITY:
            return Decision('SKIP', reason='ликвидности в стакане мало')

        # Издержки вычитаются ДО сравнения с порогом, а не после: порог,
        # применённый к валовому расхождению, пропускал бы ставки, заведомо
        # убыточные после комиссии и спреда.
        net_edge = edge - cost * price
        if net_edge <= 0:
            return Decision('SKIP', reason=f'расхождение {edge:+.3f} не покрывает '
                                           f'издержки {cost * price:.3f}')
        if net_edge * confidence < params.MIN_EDGE:
            return Decision('SKIP',
                            reason=f'чистое расхождение {net_edge * confidence:.3f} '
                                   f'ниже порога {params.MIN_EDGE}')

        kelly = kelly_fraction(model, price) * params.KELLY_FRACTION * confidence
        size = self.bankroll * kelly

        cap_position = self.bankroll * params.MAX_POSITION_PCT / 100
        size = min(size, cap_position)

        event = signal.get('event') or (signal.get('market') or {}).get('id')
        used_event = sum(p['size_usd'] for p in positions
                         if p.get('event') == event)
        room_event = self.bankroll * params.MAX_EVENT_PCT / 100 - used_event
        if room_event <= 0:
            return Decision('SKIP', kelly=kelly,
                            reason='лимит на событие исчерпан')
        size = min(size, room_event)

        category = signal.get('category') or (signal.get('market') or {}).get('feeType')
        used_cat = sum(p['size_usd'] for p in positions
                       if p.get('category') == category)
        room_cat = self.bankroll * params.MAX_CATEGORY_PCT / 100 - used_cat
        if room_cat <= 0:
            return Decision('SKIP', kelly=kelly,
                            reason='лимит на категорию исчерпан')
        size = min(size, room_cat)

        if size < 1.0:
            return Decision('SKIP', kelly=kelly,
                            reason=f'размер {size:.2f} меньше доллара')

        return Decision('PROPOSE', size_usd=round(size, 2), kelly=kelly,
                        reason=f'чистое расхождение {net_edge:+.3f}',
                        details={'event': event, 'category': category,
                                 'net_edge': net_edge, 'cost': cost * price})
