"""
Пары движка → рынки Upscale.

В движке пары записаны как у Bybit: BTCUSDT, SHIB1000USDT. У Upscale рынок
определяется базовым активом (config.baseAsset): BTC, 1000SHIB. Пары, которой
на Upscale нет (COTI, BICO), участникам не достаётся — отказ с причиной.
"""

from decimal import Decimal, ROUND_DOWN

from copier.upscale import from_fp9

# Bybit пишет множитель после тикера, Upscale — перед.
ALIASES = {
    'SHIB1000': '1000SHIB',
    'PEPE1000': '1000PEPE',
    'BONK1000': '1000BONK',
    'FLOKI1000': 'FLOKI',
}
QUOTES = ('USDT', 'USDC', 'USD')


def base_asset(pair):
    """BTCUSDT → BTC, SHIB1000USDT → 1000SHIB, BTC/USDT:USDT → BTC."""
    p = str(pair).upper().split(':')[0].replace('/', '').replace('-', '')
    for quote in QUOTES:
        if p.endswith(quote) and len(p) > len(quote):
            p = p[:-len(quote)]
            break
    return ALIASES.get(p, p)


def _cfg(market):
    return market.get('config') or {}


def market_base(market):
    return str(_cfg(market).get('baseAsset') or market.get('baseAsset') or '').upper()


def _first(market, names):
    cfg = _cfg(market)
    for name in names:
        for source in (cfg, market):
            if source.get(name) not in (None, ''):
                return source.get(name)
    return None


def _settings(market):
    return market.get('settings') or {}


def leverage_options(market, default):
    """
    Пределы плеча рынка, от большего к меньшему. Upscale отдаёт их в
    settings.maxLeverageByAccount по видам счёта ({"0": 5x, "1": 5x, "2": 2x} у BTC,
    демо 27.10.2026), а вид счёта в его ответе не назван — исполнитель пробует
    большее и на invalid_leverage берёт следующее.
    """
    by = _settings(market).get('maxLeverageByAccount') or {}
    values = {from_fp9(v) for v in (by.values() if isinstance(by, dict) else by)}
    out = sorted((v for v in values if v and v > 0), reverse=True)
    return out or [Decimal(str(default))]


def max_leverage(market, default):
    return leverage_options(market, default)[0]


def index_price(market):
    """Текущая цена рынка Upscale (state.indexPrice, fp9); None — не назвал."""
    return from_fp9((market.get('state') or {}).get('indexPrice'))


def step(market, kind):
    """Шаг цены ('price') или размера ('size'); None — рынок шаг не назвал."""
    names = {'price': ('tickSize', 'priceStep', 'priceTick'),
             'size': ('stepSize', 'sizeStep', 'lotSize', 'baseStep')}[kind]
    raw = _first(market, names)
    value = from_fp9(raw) if raw is not None else None
    return value if value and value > 0 else None


def round_down(value, step_value):
    value = Decimal(str(value))
    if not step_value:
        # Шага нет — режем до 9 знаков (предел fp9) без округления вверх.
        return value.quantize(Decimal('0.000000001'), rounding=ROUND_DOWN)
    return (value / step_value).to_integral_value(rounding=ROUND_DOWN) * step_value


def round_price(value, step_value):
    """Цена — к ближайшему шагу (стоп не уезжает дальше, чем на полшага)."""
    value = Decimal(str(value))
    if not step_value:
        return value.quantize(Decimal('0.000000001'))
    return (value / step_value).to_integral_value() * step_value


def is_open(market):
    """Рынок принимает новые позиции: не закрыт, не на паузе, не «только закрытие» (settings)."""
    s = _settings(market)
    return not (s.get('isClosed') or s.get('isPaused') or s.get('isCloseOnly'))


class Markets:
    """Справочник рынков одного счёта: базовый актив → описание рынка."""

    def __init__(self, markets):
        self.by_base = {}
        for m in markets or []:
            base = market_base(m)
            if base:
                self.by_base[base] = m

    def find(self, pair):
        return self.by_base.get(base_asset(pair))
