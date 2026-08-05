"""
Тесты агрегации дашборда.

Дашборд — единственный источник выводов месячного A/B-эксперимента. Ошибка
в разбивке по стратегиям не уронит бота и не будет видна в логах, но приведёт
к неверному решению о том, какую стратегию оставить.
"""

import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def dash(tmp_path, monkeypatch):
    """Дашборд, читающий изолированный каталог данных."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    # Режим задаётся явно, а не наследуется из .env машины. Эти тесты
    # проверяют боевую ветку сборки данных; когда оператор переключил бота на
    # фантом, build_payload() молча уходил в другую ветку и семь тестов
    # падали на пустой статистике — при том, что код был исправен. Тест,
    # зависящий от настройки рабочей машины, проверяет не то, что нужно.
    monkeypatch.setenv('TRADING_MODE', 'DEMO')
    for module in ('config', 'trade_journal', 'dashboard'):
        sys.modules.pop(module, None)

    import trade_journal
    import dashboard as dash_module

    monkeypatch.setattr(dash_module, '_broker', None, raising=False)

    monkeypatch.setattr(dash_module, 'JOURNAL_FILE', str(tmp_path / 'trades_journal.csv'))
    monkeypatch.setattr(dash_module, 'POSITIONS_FILE', str(tmp_path / 'positions_state.json'))
    monkeypatch.setattr(dash_module, 'STRATEGY_FILE', str(tmp_path / 'pair_strategy.json'))
    dash_module._columns = trade_journal.COLUMNS
    return dash_module


def write_trades(dash, rows):
    with open(dash.JOURNAL_FILE, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=dash._columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, '') for key in dash._columns})


def trade(**kwargs):
    base = dict(
        trade_id=1, strategy='FIBO', pair='BTCUSDT', direction='LONG',
        entry_price=100, stop_loss=99, tp1=104, exit_price=104,
        pnl_usd=0, pnl_pct=0, rr=4, risk_usd=10, exit_reason='TP1',
        result='WIN', open_time='2026-08-01T10:00:00',
        close_time='2026-08-01T12:00:00', duration_min=120,
    )
    base.update(kwargs)
    return base


class TestSplitByStrategy:
    def test_trades_attributed_to_their_strategy(self, dash):
        write_trades(dash, [
            trade(trade_id=1, strategy='FIBO', pnl_usd=50),
            trade(trade_id=2, strategy='SMC', pnl_usd=-10),
            trade(trade_id=3, strategy='SMC', pnl_usd=80),
        ])
        payload = dash.build_payload()

        assert payload['strategies']['FIBO']['trades'] == 1
        assert payload['strategies']['SMC']['trades'] == 2
        assert payload['strategies']['FIBO']['pnl'] == 50
        assert payload['strategies']['SMC']['pnl'] == 70

    def test_legacy_rows_without_strategy_count_as_fibo(self, dash):
        """
        Сделки, записанные до появления A/B-режима, не должны теряться:
        тогда работала только фибо-стратегия.
        """
        write_trades(dash, [trade(trade_id=1, strategy='', pnl_usd=25)])
        payload = dash.build_payload()

        assert payload['strategies']['FIBO']['trades'] == 1
        assert payload['strategies']['SMC']['trades'] == 0

    def test_open_trades_excluded_from_closed_stats(self, dash):
        """Незакрытая сделка не имеет результата и не должна попадать в итоги."""
        write_trades(dash, [
            trade(trade_id=1, pnl_usd=50),
            trade(trade_id=2, close_time='', exit_price='', pnl_usd=''),
        ])
        payload = dash.build_payload()

        assert payload['closed_total'] == 1
        assert payload['strategies']['FIBO']['trades'] == 1


class TestMetrics:
    def test_winrate_and_profit_factor(self, dash):
        write_trades(dash, [
            trade(trade_id=1, strategy='SMC', pnl_usd=100),
            trade(trade_id=2, strategy='SMC', pnl_usd=-50),
            trade(trade_id=3, strategy='SMC', pnl_usd=-50),
            trade(trade_id=4, strategy='SMC', pnl_usd=100),
        ])
        smc = dash.build_payload()['strategies']['SMC']

        assert smc['winrate'] == 50.0
        assert smc['profit_factor'] == 2.0     # 200 прибыли / 100 убытка
        assert smc['avg_win'] == 100
        assert smc['avg_loss'] == -50

    def test_profit_factor_none_without_losses(self, dash):
        """Деление на ноль недопустимо: PF без убытков не определён."""
        write_trades(dash, [trade(trade_id=1, pnl_usd=10)])
        assert dash.build_payload()['strategies']['FIBO']['profit_factor'] is None

    def test_sum_r_normalises_by_risk(self, dash):
        """
        Сумма R не зависит от размера депозита и риска на сделку — только она
        позволяет честно сравнить стратегии, если риск у них разный.
        """
        write_trades(dash, [
            trade(trade_id=1, strategy='SMC', pnl_usd=30, risk_usd=10),   # +3R
            trade(trade_id=2, strategy='SMC', pnl_usd=-100, risk_usd=100),  # -1R
        ])
        smc = dash.build_payload()['strategies']['SMC']

        assert smc['sum_r'] == 2.0
        assert smc['expectancy_r'] == 1.0

    def test_equity_curve_accumulates_in_time_order(self, dash):
        write_trades(dash, [
            trade(trade_id=2, strategy='SMC', pnl_usd=40,
                  close_time='2026-08-02T12:00:00'),
            trade(trade_id=1, strategy='SMC', pnl_usd=-10,
                  close_time='2026-08-01T12:00:00'),
        ])
        curve = dash.build_payload()['equity']['SMC']

        assert [point[1] for point in curve] == [-10.0, 30.0]


class TestOpenPositions:
    def test_open_positions_carry_owner_strategy(self, dash):
        write_trades(dash, [])
        with open(dash.POSITIONS_FILE, 'w', encoding='utf-8') as fh:
            json.dump({'XRPUSDT': {
                'pair': 'XRPUSDT', 'direction': 'LONG', 'zone': 'ORDER_BLOCK',
                'entry_time': '2026-08-03T12:00:00', 'entry_price': 0.55,
                'stop_loss': 0.54, 'take_profit_1': 0.58,
                'take_profit_2': 0.60, 'rr': 3.5, 'risk_amount': 50,
            }}, fh)
        with open(dash.STRATEGY_FILE, 'w', encoding='utf-8') as fh:
            json.dump({'XRPUSDT': 'SMC'}, fh)

        payload = dash.build_payload()
        assert payload['open_positions'][0]['strategy'] == 'SMC'
        assert payload['strategies']['SMC']['open'] == 1
        assert payload['strategies']['FIBO']['open'] == 0

    def test_missing_files_do_not_crash(self, dash):
        """Свежая установка: файлов ещё нет — дашборд обязан открыться."""
        payload = dash.build_payload()

        assert payload['open_positions'] == []
        assert payload['closed_total'] == 0
        assert payload['strategies']['FIBO']['trades'] == 0


# ── Фантомный режим ──────────────────────────────────────────────────────────

class FakeBroker:
    """Брокер с фиксированным состоянием: дашборду хватает snapshot()."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


@pytest.fixture()
def paper_dash(tmp_path, monkeypatch):
    """Дашборд в фантомном режиме, читающий изолированный каталог."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TRADING_MODE', 'PAPER')
    for module in ('config', 'paper_broker', 'dashboard'):
        sys.modules.pop(module, None)

    import paper_broker
    import dashboard as dash_module

    monkeypatch.setattr(dash_module, 'PAPER_JOURNAL', str(tmp_path / 'paper_trades.csv'))
    dash_module._paper_columns = paper_broker.COLUMNS
    return dash_module


def write_paper(dash, rows):
    import paper_broker
    with open(dash.PAPER_JOURNAL, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=paper_broker.COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, '') for key in paper_broker.COLUMNS})


def paper_trade(**kwargs):
    base = dict(
        trade_id=1, strategy='FIBO', pair='BTCUSDT', direction='LONG',
        entry_price=100, stop_loss=90, tp1=130, exit_price=130,
        pnl_usd=300, pnl_r=3.0, pnl_pct=3.0, rr=3, risk_usd=100,
        fees_usd=1.5, funding_usd=0.4, exit_reason='TP1', result='WIN',
        open_time='2026-08-01T10:00:00', close_time='2026-08-01T12:00:00',
        duration_min=120, balance_after=10300, why='LONG в зоне Zone_A',
    )
    base.update(kwargs)
    return base


class TestPaperDashboard:
    def test_return_measured_against_own_deposit(self, paper_dash):
        """
        Депозиты у стратегий разные, поэтому сравнивать доллары нельзя:
        доходность обязана считаться в процентах от своей базы.
        """
        write_paper(paper_dash, [
            paper_trade(trade_id=1, strategy='FIBO', pnl_usd=500),
            paper_trade(trade_id=2, strategy='SMC', pnl_usd=500),
        ])
        paper_dash._broker = FakeBroker({
            'started_at': '2026-08-01T00:00:00',
            'strategies': {
                'FIBO': {'start_balance': 10_000, 'balance': 10_500, 'equity': 10_500},
                'SMC': {'start_balance': 5_000, 'balance': 5_500, 'equity': 5_500},
            },
            'open': [],
        })
        payload = paper_dash.build_payload()

        assert payload['paper'] is True
        assert payload['strategies']['FIBO']['return_pct'] == 5.0
        assert payload['strategies']['SMC']['return_pct'] == 10.0

    def test_costs_are_visible(self, paper_dash):
        """Комиссии и фандинг должны попадать в сводку, а не растворяться в PnL."""
        write_paper(paper_dash, [
            paper_trade(trade_id=1, fees_usd=2.0, funding_usd=0.5),
            paper_trade(trade_id=2, fees_usd=3.0, funding_usd=-0.5),
        ])
        paper_dash._broker = FakeBroker({'strategies': {'FIBO': {
            'start_balance': 10_000, 'balance': 10_600, 'equity': 10_600}}, 'open': []})
        fibo = paper_dash.build_payload()['strategies']['FIBO']

        assert fibo['fees'] == 5.0
        assert fibo['funding'] == 0.0

    def test_drawdown_measured_from_peak(self, paper_dash):
        write_paper(paper_dash, [
            paper_trade(trade_id=1, pnl_usd=2000, close_time='2026-08-01T10:00:00'),
            paper_trade(trade_id=2, pnl_usd=-3000, close_time='2026-08-02T10:00:00'),
        ])
        paper_dash._broker = FakeBroker({'strategies': {'FIBO': {
            'start_balance': 10_000, 'balance': 9_000, 'equity': 9_000}}, 'open': []})
        fibo = paper_dash.build_payload()['strategies']['FIBO']

        # Пик 12 000 -> дно 9 000 = просадка 25%, а не 10% от старта
        assert fibo['max_dd_pct'] == 25.0

    def test_open_and_waiting_are_separate(self, paper_dash):
        """
        Позиция в рынке и ордер, ждущий активации, — разные вещи: у первой
        есть плавающий результат, у второго нет ничего, кроме шанса
        исполниться. В одной таблице они путали бы картину.
        """
        write_paper(paper_dash, [])
        paper_dash._broker = FakeBroker({
            'strategies': {'SMC': {'start_balance': 10_000, 'balance': 10_000,
                                   'equity': 10_120}},
            'open': [
                {'strategy': 'SMC', 'pair': 'ETHUSDT', 'direction': 'LONG',
                 'opened': '2026-08-03T10:00:00', 'unrealised': 120.0,
                 'why': 'LONG от зоны ORDER_BLOCK'},
            ],
            'pending': [
                {'strategy': 'SMC', 'pair': 'SOLUSDT', 'direction': 'SHORT',
                 'opened': '2026-08-03T11:00:00', 'pending': True,
                 'distance_pct': 0.42, 'why': ''},
            ],
        })
        payload = paper_dash.build_payload()

        assert payload['strategies']['SMC']['open'] == 1
        assert payload['strategies']['SMC']['pending'] == 1
        assert payload['open_positions'][0]['why'] == 'LONG от зоны ORDER_BLOCK'
        assert payload['pending_orders'][0]['pair'] == 'SOLUSDT'

    def test_floating_result_reported_per_strategy(self, paper_dash):
        """Без плавающего итога депозит на карточке расходится с суммой сделок."""
        write_paper(paper_dash, [])
        paper_dash._broker = FakeBroker({
            'strategies': {'SMC': {'start_balance': 10_000, 'balance': 10_000,
                                   'equity': 10_090}},
            'open': [
                {'strategy': 'SMC', 'pair': 'ETHUSDT', 'direction': 'LONG',
                 'opened': '2026-08-03T10:00:00', 'unrealised': 120.0, 'why': ''},
                {'strategy': 'SMC', 'pair': 'BTCUSDT', 'direction': 'SHORT',
                 'opened': '2026-08-03T12:00:00', 'unrealised': -30.0, 'why': ''},
            ],
            'pending': [],
        })
        assert paper_dash.build_payload()['strategies']['SMC']['floating'] == 90.0

    def test_equity_curve_carries_percent(self, paper_dash):
        """Третье значение точки — доходность в %, иначе кривые двух депозитов несравнимы."""
        write_paper(paper_dash, [
            paper_trade(trade_id=1, pnl_usd=1000, close_time='2026-08-01T10:00:00'),
            paper_trade(trade_id=2, pnl_usd=1000, close_time='2026-08-02T10:00:00'),
        ])
        paper_dash._broker = FakeBroker({'strategies': {'FIBO': {
            'start_balance': 10_000, 'balance': 12_000, 'equity': 12_000}}, 'open': []})
        curve = paper_dash.build_payload()['equity']['FIBO']

        assert [point[1] for point in curve] == [11_000.0, 12_000.0]
        assert [point[2] for point in curve] == [10.0, 20.0]

    def test_reset_excludes_old_trades_from_statistics(self, paper_dash):
        """
        После смены депозита прежние сделки считались от другой базы. Оставить
        их в статистике значит смешать доходность от двух разных депозитов и
        получить процент, которого не было ни при одном из них.
        """
        write_paper(paper_dash, [
            paper_trade(trade_id=1, pnl_usd=900, close_time='2026-08-01T10:00:00'),
            paper_trade(trade_id=2, pnl_usd=100, close_time='2026-08-03T10:00:00'),
        ])
        paper_dash._broker = FakeBroker({'strategies': {'FIBO': {
            'start_balance': 50_000, 'balance': 50_100, 'equity': 50_100,
            'reset_at': '2026-08-02T00:00:00'}}, 'open': [], 'pending': []})
        fibo = paper_dash.build_payload()['strategies']['FIBO']

        assert fibo['trades'] == 1              # только сделка после перезапуска
        assert fibo['pnl'] == 100
        assert fibo['dropped_before_reset'] == 1
        assert fibo['return_pct'] == 0.2        # 100 от 50 000, а не 1000 от 10 000

    def test_history_stays_in_export_after_reset(self, paper_dash):
        """Исключённые из статистики сделки обязаны остаться в выгрузке."""
        write_paper(paper_dash, [
            paper_trade(trade_id=1, close_time='2026-08-01T10:00:00'),
            paper_trade(trade_id=2, close_time='2026-08-03T10:00:00'),
        ])
        paper_dash._broker = FakeBroker({'strategies': {'FIBO': {
            'start_balance': 10_000, 'balance': 10_000, 'equity': 10_000,
            'reset_at': '2026-08-02T00:00:00'}}, 'open': [], 'pending': []})

        assert paper_dash.build_payload()['closed_total'] == 2

    def test_works_without_live_broker(self, paper_dash):
        """Дашборд можно открыть отдельно от бота — история читается из файла."""
        write_paper(paper_dash, [paper_trade(trade_id=1, pnl_usd=300,
                                             balance_after=10_300)])
        paper_dash._broker = None
        payload = paper_dash.build_payload()

        assert payload['closed_total'] == 1
        assert payload['strategies']['FIBO']['start_balance'] == 10_000.0
