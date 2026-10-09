"""
Статистика стратегий и их счетов (accounts/stats.py) — реорганизация, этап 6.

Страница «Стратегии» показывает: сколько сетапов дала стратегия, сколько дошло
до входа, сколько отработало в плюс и в минус, качество сделок, разрезы, что
не стало сделкой и чем кончилось бы. Здесь — что числа считаются верно на
строках журнала сетапов (setup_journal) и что панель их отдаёт.
"""

import os
import sys


import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution import setup_journal as sj  # noqa: E402
from accounts import stats  # noqa: E402

NOW = 1_800_000_000


def trade(r, usd=None, side='LONG', pair='BTCUSDT', reason='стоп-лосс', closed='2026-10-01T10:00:00+00:00',
          minutes=60, mfe=None, mae=None, ago_days=1):
    outcome = 'плюс' if r > 0 else ('минус' if r < 0 else 'ноль')
    return {'stage': sj.TRADE, 'outcome': outcome, 'pnl_r': r, 'pnl_usd': usd if usd is not None else r * 100,
            'direction': side, 'pair': pair, 'exit_reason': reason, 'closed_at': closed,
            'duration_min': minutes, 'mfe_r': mfe, 'mae_r': mae, '_placed': NOW - ago_days * 86400}


def row(stage, outcome='', gate='', pnl_r=None, ago_days=1):
    return {'stage': stage, 'outcome': outcome, 'gate': gate, 'pnl_r': pnl_r, '_placed': NOW - ago_days * 86400}


class TestFunnel:
    def test_from_setup_to_result(self):
        rows = [trade(2.0), trade(-1.0), trade(-1.0), row(sj.OPEN), row(sj.PENDING),
                row(sj.DROPPED, 'цель без входа'), row(sj.SHADOW, 'стоп', 'предел издержек', -1.0),
                row(sj.PLAN_REFUSED)]
        f = stats.funnel(rows)
        assert f == {'setups': 8, 'orders': 6, 'filled': 4, 'closed': 3, 'open': 1, 'pending': 1,
                     'wins': 1, 'losses': 2, 'flat': 0, 'dropped': 1, 'refused': 1, 'plans': 1}


class TestQuality:
    def test_numbers(self):
        q = stats.quality([trade(2.0, closed='2026-10-01'), trade(-1.0, closed='2026-10-02'),
                           trade(-1.0, closed='2026-10-03'), trade(3.0, closed='2026-10-04', minutes=120)])
        assert q['trades'] == 4 and q['wins'] == 2 and q['losses'] == 2
        assert q['win_rate'] == 0.5
        assert q['total_r'] == 3.0 and q['avg_r'] == 0.75
        assert q['profit_factor'] == 2.5                 # 5R плюсов ÷ 2R минусов
        assert q['max_dd_r'] == 2.0                      # +2 → 0: два минуса подряд
        assert q['max_loss_streak'] == 2
        assert q['best_r'] == 3.0 and q['worst_r'] == -1.0
        assert q['median_minutes'] == 60

    def test_curve_follows_the_exit_order_not_the_list_order(self):
        """Просадка — по времени выхода: порядок строк в журнале — по сетапу."""
        late_loss = trade(-1.0, closed='2026-10-05')
        q = stats.quality([late_loss, trade(1.0, closed='2026-10-01'), trade(-1.0, closed='2026-10-04')])
        assert q['max_dd_r'] == 2.0 and q['max_loss_streak'] == 2

    def test_no_trades(self):
        q = stats.quality([])
        assert q['trades'] == 0 and q['win_rate'] is None and q['profit_factor'] is None


class TestBreakdownAndPeriod:
    def test_by_side(self):
        sides = stats.breakdown([trade(2.0, side='LONG'), trade(-1.0, side='SHORT'), trade(1.0, side='LONG')],
                                lambda t: t['direction'])
        assert [(g['name'], g['trades'], g['total_r'], g['win_rate']) for g in sides] == [
            ('LONG', 2, 3.0, 1.0), ('SHORT', 1, -1.0, 0.0)]

    def test_period_cuts_by_setup_time(self):
        rows = [trade(1.0, ago_days=3), trade(-1.0, ago_days=40)]
        assert len(stats.window(rows, 30, now=NOW)) == 1
        assert len(stats.window(rows, None, now=NOW)) == 2

    def test_report_has_every_block(self):
        rep = stats.report('FIBO', [trade(1.0), trade(-1.0, pair='ETHUSDT'), row(sj.DROPPED, 'не налилась за срок')],
                           days=30, now=NOW)
        for key in ('funnel', 'quality', 'sides', 'exits', 'months', 'pairs_best', 'pairs_worst', 'not_traded'):
            assert key in rep, key
        assert rep['not_traded']['dropped'] == {'не налилась за срок': 1}


class TestNotTraded:
    def test_refusals_with_what_they_would_have_done(self):
        """Минус «было бы» — предел уберёг счёт; незавершённая тень в итог не идёт."""
        rows = [row(sj.SHADOW, 'стоп', 'толпа за сделку', -1.0),
                row(sj.SHADOW, 'цель', 'толпа за сделку', 3.0),
                row(sj.SHADOW, '', 'толпа за сделку', 0.4),
                row(sj.SHADOW, 'стоп', 'направленный кэп', -1.0)]
        refused = stats.not_traded(rows)['refused']
        crowd = refused[0]
        assert crowd['gate'] == 'толпа за сделку' and crowd['count'] == 3 and crowd['finished'] == 2
        assert crowd['would_r'] == 2.0
        assert crowd['outcomes'] == {'стоп': 1, 'цель': 1, 'наблюдается': 1}


class FakeBroker:
    def __init__(self):
        self._pos = {'BTCUSDT': {'entry_price': 100.0, 'stop_loss': 98.0, 'size': 10.0, 'risk_amount': 20.0}}
        self._pend = {'ETHUSDT': {'risk_amount': 15.0}}

    def start_balance(self, s):
        return 10_000.0

    def balance(self, s):
        return 10_100.0

    def equity(self, s):
        return 10_200.0

    def positions(self, s):
        return self._pos

    def pending(self, s):
        return self._pend

    @staticmethod
    def _live_risk(pos):
        return abs(pos['entry_price'] - pos['stop_loss']) * pos['size']

    def reset_at(self, s):
        return None


class TestAccount:
    def test_money_and_settings(self):
        acc = stats.account('FIBO', FakeBroker(), [trade(-2.0, usd=-200, closed='2026-10-01'),
                                                   trade(3.0, usd=300, closed='2026-10-02')])
        assert acc['start'] == 10_000.0 and acc['equity'] == 10_200.0
        assert acc['return_pct'] == 2.0
        assert acc['max_dd_pct'] == 2.0                  # 10 000 → 9 800
        assert acc['open_risk'] == 35.0                  # позиция 20 + заявка 15
        assert acc['positions'] == 1 and acc['pending'] == 1
        assert acc['settings']['risk_pct'] > 0

    def test_without_broker_only_settings(self):
        acc = stats.account('SMC')
        assert set(acc) == {'settings'}


class TestPanel:
    def test_endpoints_answer(self, monkeypatch):
        dashboard = __import__('importlib').import_module('control.dashboard')
        monkeypatch.setattr(dashboard, '_broker', None)
        monkeypatch.setattr(dashboard, '_report_cache', {})
        monkeypatch.setattr(dashboard, '_strategy_rows', lambda strategy: [trade(1.0), trade(-1.0)])
        rep = dashboard._strategy_report('FIBO', None)
        assert rep['funnel']['closed'] == 2 and 'account' in rep
        cmp = dashboard._strategy_compare(30.0)
        assert {x['strategy'] for x in cmp['strategies']} >= {'FIBO', 'SMC'}
        with pytest.raises(ValueError):
            dashboard._strategy_report('НЕТ', None)

    def test_period_from_the_query(self):
        dashboard = __import__('importlib').import_module('control.dashboard')
        assert dashboard._period('30') == 30.0
        assert dashboard._period('') is None and dashboard._period('all') is None
        assert dashboard._period('0') is None and dashboard._period('-5') is None

    def test_the_page_is_in_the_menu(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'control', 'panel', 'js')
        app = open(os.path.join(root, 'app.js'), encoding='utf-8').read()
        page = open(os.path.join(root, 'pages', 'strategies.js'), encoding='utf-8').read()
        assert "id: 'strategies'" in app and 'page: strategies' in app
        assert '/api/strategy_report' in page and '/api/strategy_compare' in page


def test_the_cache_is_short():
    """Журнал собирается из нескольких файлов — но цифры не должны стареть
    дольше минуты."""
    dashboard = __import__('importlib').import_module('control.dashboard')
    assert dashboard.REPORT_TTL_S <= 120
