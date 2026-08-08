"""
Прогон всех трёх стратегий: сбор рынков, сигналы, решения, запись.

КАЖДАЯ СТРАТЕГИЯ СО СВОИМ СЧЁТОМ И СВОИМИ ЛИМИТАМИ — раздел 17 спецификации.
Смешав капитал, мы не смогли бы сказать, какая заработала, а какая проела: у
них разная частота, разный профиль просадки и разный статус проверки. Общий
счёт превратил бы сравнение в гадание.

ЗАПИСЫВАЮТСЯ И ОТКАЗЫ ТОЖЕ. Без них нельзя отличить «сигналов не было» от
«сигналы были, но все отсеяны», а это разные болезни: первая лечится
расширением списка рынков, вторая — порогами.

ЖИВОЕ ИСПОЛНЕНИЕ ЗДЕСЬ НЕ ДЕЛАЕТСЯ ВОВСЕ. Модуль считает и предлагает; отправка
ордера — отдельный слой, которого пока нет намеренно. Ни одна из трёх стратегий
не имеет пройденной валидации, и строить исполнение раньше проверки значило бы
повторить ту самую ошибку, от которой предостерегает сама спецификация.
"""

from . import client, params, store
from .longshot import LongshotSignal
from .risk import RiskManager
from .signal_crypto import CryptoSignal
from .weather import WeatherSignal

# Каждая стратегия говорит, какие рынки ей нужны. Тянуть весь список для всех
# трёх — лишние запросы и лишний шум в отборе.
MODULES = (
    (WeatherSignal, ('weather_fees',)),
    (CryptoSignal, ('crypto_fees_v2', 'finance_prices_fees')),
    (LongshotSignal, None),          # None — все категории
)


def collect_markets(fee_types, pages=15):
    """Активные рынки нужных категорий. None — берём все."""
    if fee_types is None:
        return client.active_markets(limit_pages=pages)
    out = []
    for fee_type in fee_types:
        out += client.active_markets(limit_pages=pages, fee_type=fee_type)
    return out


def run_once(total_bankroll=None, day_losses=None, open_positions=None,
             modules=None):
    """
    Один проход по всем стратегиям.

    Возвращает список предложений. Ничего не отправляет и ничего не решает за
    человека: при MANUAL_CONFIRM предложения только показываются.
    """
    losses = dict(day_losses or {})
    positions = list(open_positions or [])
    proposals = []

    for factory, fee_types in (modules or MODULES):
        module = factory()
        markets = collect_markets(fee_types)
        if not markets:
            continue
        results = module.scan(markets)
        store.save_snapshot([
            {'market': r.market, 'price': r.market_probability,
             'model': r.model_probability, 'edge': r.edge, 'cost': r.cost,
             'liquidity': r.liquidity, 'city': None, 'forecast_c': None}
            for r in results])

        manager = RiskManager(
            bankroll=params.bankroll_for(module.name, total_bankroll),
            day_loss_usd=losses.get(module.name, 0.0))

        for result in results:
            signal = result.as_risk_input(module.name)
            mine = [p for p in positions if p.get('strategy') == module.name]
            decision = manager.evaluate(signal, mine)
            if decision.action == 'PROPOSE' and not module.allows_real_money():
                # Пометка ставится ЗДЕСЬ, а не в каждом модуле: забыть её в
                # одном из трёх — вопрос времени, а последствие — реальная
                # сделка по непроверенной стратегии.
                decision.details['paper_only'] = True
                decision.details['validation'] = module.validation.status
                decision.reason = f'ТОЛЬКО БУМАГА: {decision.reason}'
            store.save_decision(signal, decision, module.name)
            if decision.action == 'PROPOSE':
                proposals.append({'strategy': module.name, 'signal': signal,
                                  'decision': decision, 'note': result.note})
    return proposals


def summary(proposals):
    """Короткая сводка для показа человеку."""
    if not proposals:
        return 'предложений нет'
    lines = []
    for p in proposals:
        market = p['signal'].get('market') or {}
        lines.append(
            f'[{p["strategy"]}] {str(market.get("question"))[:58]}\n'
            f'    цена {p["signal"]["price"]:.3f}  наша {p["signal"]["model"]:.3f}'
            f'  ставка ${p["decision"].size_usd:.2f}  {p["decision"].reason}')
    return '\n'.join(lines)
