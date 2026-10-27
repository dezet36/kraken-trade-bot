"""
Размер и ворота перед заявкой участника. Своя реализация по образцу
accounts/books.decide торгового бота — без импорта (копировщик от бота не зависит).

Правила (CLAUDE.md, «Стоп» и «Риск»):
  - стоп мастера — место инвалидации, он не двигается ради размера: если
    номинал не влезает в баланс × плечо — отказ, а не сдвиг стопа;
  - риск задаёт счёт участника (% от текущего баланса), не стратегия;
  - сделку, чей худший исход вместе с открытыми нарушит правила пропа, не берут.

Всё в Decimal; на входе — числа мастера и статус риска счёта Upscale
(суммы fp9 уже переведены вызывающим в Decimal).
"""

from dataclasses import dataclass, field
from decimal import Decimal

from copier import markets as mk

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def D(value, default=None):
    if value is None or value == '':
        return default
    try:
        return Decimal(str(value))
    except Exception:                                  # noqa: BLE001
        return default


@dataclass
class Decision:
    ok: bool
    reason: str = ''
    base_size: Decimal = ZERO
    leverage: Decimal = ZERO
    margin: Decimal = ZERO
    reserve: Decimal = ZERO          # amount заявки: маржа + запас на комиссию, спред и проскальзывание
    risk_usd: Decimal = ZERO
    limit_price: Decimal = ZERO
    stop: Decimal = ZERO
    take: Decimal = None
    details: dict = field(default_factory=dict)


def refuse(reason, **details):
    return Decision(False, reason, details=details)


def open_risk(trades):
    """
    Худший исход открытого: сумма size × |вход − стоп| по позициям и заявкам
    счёта. Позиция в безубытке (стоп по ту сторону входа) риска не несёт.
    """
    total = ZERO
    for t in trades:
        size = D(t.get('base_size'), ZERO)
        entry = D(t.get('entry_price')) or D(t.get('limit_price'), ZERO)
        stop = D(t.get('stop'), ZERO)
        long_ = str(t.get('direction')).upper() == 'LONG'
        loss = (entry - stop) if long_ else (stop - entry)
        if loss > 0:
            total += size * loss
    return total


def headroom(status, buffer_pct):
    """
    Сколько ещё можно потерять до ближайшего лимита пропа, $ (None — лимитов нет).
    status — снимок счёта: currentEquity, dayStartEquity, initialAccountBalance,
    maxDailyDrawdown %, maxTotalDrawdown %, maxTrailingTotalDrawdown %, maxEquity,
    maxDrawdownStop (уровень эквити провала, если счёт его назвал).
    """
    equity = D(status.get('currentEquity'))
    if equity is None:
        return None, 'нет эквити счёта'
    initial = D(status.get('initialAccountBalance')) or equity
    buffer = initial * D(buffer_pct, ZERO) / HUNDRED
    floors = []
    daily = D(status.get('maxDailyDrawdown'))
    day_start = D(status.get('dayStartEquity'))
    if daily and day_start:
        floors.append(('дневной лимит', day_start * (1 - daily / HUNDRED)))
    stop_level = D(status.get('maxDrawdownStop'))
    if stop_level:
        floors.append(('общий лимит', stop_level))
    else:
        total = D(status.get('maxTotalDrawdown'))
        base = D(status.get('drawdownBase')) or initial
        if total:
            floors.append(('общий лимит', base * (1 - total / HUNDRED)))
    trailing = D(status.get('maxTrailingTotalDrawdown'))
    max_eq = D(status.get('maxEquity'))
    if trailing and max_eq:
        floors.append(('скользящий лимит', max_eq * (1 - trailing / HUNDRED)))
    if not floors:
        return None, ''
    name, floor = max(floors, key=lambda f: f[1])
    return equity - floor - buffer, name


def plan_entry(order, market, status, rules, open_trades, other_sides, default_leverage,
               buffer_pct, leverage=None, reserve_extra_pct=0):
    """
    order       — заявка мастера: direction, limit_price, stop_loss, targets.
    market      — рынок Upscale (None — пары на Upscale нет).
    status      — снимок счёта (Decimal): status, tradingLockReason, accountBalance,
                  currentEquity и поля лимитов (см. headroom).
    rules       — risk_pct: риск на сделку из правил счёта-мастера в боте.
    open_trades — открытые сделки и заявки копировщика на этом счёте.
    other_sides — стороны по этому рынку на ДРУГИХ счетах участника (хедж запрещён).
    """
    direction = str(order.get('direction')).upper()
    long_ = direction == 'LONG'
    if market is None:
        return refuse('пары нет на Upscale')
    if not mk.is_open(market):
        return refuse('рынок Upscale закрыт или только на закрытие')
    if str(status.get('status') or '').lower() != 'active':
        return refuse(f'счёт не активен ({status.get("status")})')
    if status.get('tradingLockReason'):
        return refuse('счёт заблокирован: ' + ', '.join(map(str, status.get('tradingLockReason'))))

    market_id = str(market.get('id'))
    if any(str(t.get('market_id')) == market_id for t in open_trades):
        return refuse('по этой паре на счёте уже есть сделка копировщика')
    # Правило Upscale, а не фильтр риска: встречные позиции на разных счетах одного трейдера запрещены.
    opposite = 'SHORT' if long_ else 'LONG'
    if opposite in {str(s).upper() for s in other_sides}:
        return refuse('на другом счёте участника встречная позиция (хедж между счетами запрещён)')

    price_step = mk.step(market, 'price')
    limit_price = mk.round_price(order['limit_price'], price_step)
    stop = mk.round_price(order['stop_loss'], price_step)
    if limit_price <= 0 or stop <= 0:
        return refuse('цена заявки или стопа не положительна')
    dist = (limit_price - stop) if long_ else (stop - limit_price)
    if dist <= 0:
        return refuse('стоп по ту сторону входа')

    balance = D(status.get('accountBalance')) or D(status.get('currentEquity'))
    if not balance or balance <= 0:
        return refuse('нет баланса счёта')
    risk_pct = D(rules.get('risk_pct'), ZERO)
    if risk_pct <= 0:
        return refuse('риск счёта-источника неизвестен (нет книги из бота)')
    risk_usd = balance * risk_pct / HUNDRED

    room, limit_name = headroom(status, buffer_pct)
    if room is not None:
        worst = open_risk(open_trades) + risk_usd
        if worst > room:
            return refuse(f'{limit_name}: худший исход ${worst:.2f} при запасе ${room:.2f}',
                          worst=float(worst), room=float(room))

    base_size = mk.round_down(risk_usd / dist, mk.step(market, 'size'))
    if base_size <= 0:
        return refuse('размер меньше шага рынка')
    notional = base_size * limit_price
    leverage = D(leverage) or mk.max_leverage(market, default_leverage)
    margin = notional / leverage
    reserve = margin * (1 + D(reserve_extra_pct, ZERO) / HUNDRED)
    if reserve > balance:
        return refuse(f'номинал ${notional:.0f} с запасом резерва больше баланс × плечо '
                      f'${balance * leverage:.0f} (стоп {dist / limit_price * 100:.2f}% — теснее, '
                      f'чем позволяет плечо)')

    targets = [D(t) for t in (order.get('targets') or []) if D(t)]
    take = mk.round_price(targets[0], price_step) if len(targets) == 1 else None
    return Decision(True, '', base_size=base_size, leverage=leverage,
                    margin=margin, reserve=reserve, risk_usd=risk_usd, limit_price=limit_price,
                    stop=stop, take=take,
                    details={'notional': float(notional), 'dist_pct': float(dist / limit_price * 100)})


def split_targets(base_size, targets, fractions, size_step):
    """
    Доли позиции по целям: [(цель, размер)]. Остаток от округления — на
    последнюю цель, чтобы вся позиция была закрыта целями.
    """
    targets = [D(t) for t in targets or []]
    fractions = [D(f, ZERO) for f in fractions or []]
    if not targets:
        return []
    if len(fractions) != len(targets) or sum(fractions) <= 0:
        fractions = [Decimal(1) / len(targets)] * len(targets)
    total = sum(fractions)
    parts, used = [], ZERO
    for i, (target, frac) in enumerate(zip(targets, fractions)):
        if i == len(targets) - 1:
            size = base_size - used
        else:
            size = mk.round_down(base_size * frac / total, size_step)
        if size > 0:
            parts.append((target, size))
            used += size
    return parts
