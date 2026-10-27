from decimal import Decimal

from copier import sizing
from copier.upscale import to_fp9

from tests.helpers import market as _market

MARKET = _market('m-btc', 'BTC')


def status(**over):
    st = {'status': 'active', 'tradingLockReason': None, 'accountBalance': Decimal(10_000),
          'currentEquity': Decimal(10_000), 'dayStartEquity': Decimal(10_000),
          'initialAccountBalance': Decimal(10_000), 'maxDailyDrawdown': Decimal(5),
          'maxTotalDrawdown': Decimal(10)}
    st.update(over)
    return st


def order(direction='LONG', limit=100.0, stop=95.0, targets=(110.0,)):
    return {'direction': direction, 'limit_price': limit, 'stop_loss': stop, 'targets': list(targets)}


def plan(o=None, st=None, rules=None, open_trades=(), sides=(), market=MARKET):
    return sizing.plan_entry(o or order(), market, st or status(), rules or {'risk_pct': 1},
                             list(open_trades), set(sides), 5, 0.5)


def test_size_from_risk_and_stop():
    d = plan()
    assert d.ok
    assert d.risk_usd == Decimal(100)
    assert d.base_size == Decimal(20)            # 100 $ / 5 $ дистанции
    assert d.take == Decimal(110)
    assert d.leverage == Decimal(5)


def test_short_and_multiple_targets_have_no_attached_take():
    d = plan(order('SHORT', 100, 104, targets=(95, 90)))
    assert d.ok and d.base_size == Decimal(25) and d.take is None


def test_stop_on_wrong_side_refused():
    assert not plan(order('LONG', 100, 101)).ok


def test_leverage_cap_refuses_instead_of_moving_stop():
    d = plan(order('LONG', 100, 99.9))           # 0.1% стоп → номинал в 10 раз больше баланса
    assert not d.ok and 'плечо' in d.reason


def test_daily_headroom():
    # Сегодня уже −4.6% от начала дня: до лимита 5% и буфера 0.5% места нет.
    d = plan(st=status(currentEquity=Decimal(9_540)))
    assert not d.ok and 'дневной' in d.reason


def test_open_risk_counts():
    open_trades = [{'base_size': '40', 'limit_price': '100', 'stop': '95', 'direction': 'LONG',
                    'market_id': 'm-eth'}] * 2   # 2 × 200 $ риска
    d = plan(st=status(currentEquity=Decimal(9_700)), open_trades=open_trades)
    assert not d.ok


def test_breakeven_position_has_no_risk():
    open_trades = [{'base_size': '40', 'entry_price': '100', 'stop': '100.5', 'direction': 'LONG',
                    'market_id': 'm-eth'}]
    assert sizing.open_risk(open_trades) == 0


def test_hedge_across_accounts_refused():
    d = plan(sides={'SHORT'})
    assert not d.ok and 'хедж' in d.reason


def test_one_trade_per_market():
    d = plan(open_trades=[{'market_id': 'm-btc', 'direction': 'LONG', 'base_size': '1',
                           'limit_price': '100', 'stop': '95'}])
    assert not d.ok


def test_no_source_risk_refused():
    d = plan(rules={'risk_pct': None})
    assert not d.ok and 'риск счёта-источника' in d.reason


def test_no_market_and_locked_account():
    assert not plan(market=None).ok
    assert not plan(st=status(status='trading_locked_daily')).ok
    assert not plan(st=status(tradingLockReason=['auto'])).ok


def test_split_targets_rest_goes_last():
    parts = sizing.split_targets(Decimal('10'), [110, 120, 130], [0.25, 0.25, 0.5], Decimal('0.3'))
    sizes = [p[1] for p in parts]
    assert sum(sizes) == Decimal('10')
    assert sizes[0] == Decimal('2.4')


def test_real_upscale_status_lets_trades_through():
    """
    Ответ /accounts/with-risk-status демо-счёта 27.10.2026 (как есть). Раньше правила
    читались как fp9 → дневной лимит 0.0000005% → «запас $-50» и отказ на каждый вход.
    """
    from copier.follower import status_from_account
    raw = {'type': 'demo', 'status': 'active', 'currentPhase': 'active_evaluation',
           'initialAccountBalance': '10000000000000', 'accountBalance': '10000000000000',
           'maxDailyDrawdown': '5', 'maxTotalDrawdown': '10', 'maxTrailingTotalDrawdown': None,
           'profitTarget': '5', 'drawdownBase': None, 'maxDrawdownStop': None, 'minTradingDays': 3,
           'antifraudStatus': 'normal',
           'riskStatus': {'currentBalance': '10000000000000', 'currentEquity': '10000000000000',
                          'dayStartEquity': '10000000000000', 'maxEquity': None,
                          'consistencyRuleRatio': None, 'consistencyRuleLimit': '300000000'}}
    st = status_from_account(raw)
    assert st['maxDailyDrawdown'] == 5 and st['maxTotalDrawdown'] == 10 and st['profitTarget'] == 5
    assert st['consistencyRuleLimit'] == 30
    room, name = sizing.headroom(st, 0.5)
    assert room == Decimal(450) and name == 'дневной лимит'      # 10 000 − 9 500 − 50
    d = sizing.plan_entry(order(), MARKET, st, {'risk_pct': 1}, [], set(), 5, 0.5)
    assert d.ok and d.risk_usd == 100
