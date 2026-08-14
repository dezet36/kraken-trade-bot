"""
«Вложено» берётся из состояния, а не из настроек.

ЧТО ВИДЕЛ ЧЕЛОВЕК НА ЖИВЫХ ДАННЫХ:

    FIBO   вложено $20 000 → стало $10 076   итог -$9 924
    по журналу сделок:                        итог   +$112

    SMC    вложено  $6 800 → стало  $9 764   итог +$2 964
    по журналу сделок:                        итог   -$236

У обеих знак перевёрнут, и суммы не имеют отношения к торговле: убыток в шесть
тысяч там, где бот отработал в небольшой плюс.

ПРИЧИНА. Настройка говорит, с какой суммой НАЧАТЬ. Брокер, найдя начатый
эксперимент, оставляет прежний депозит — иначе доходность считалась бы от
подменённого знаменателя, — и даже предупреждает об этом в журнале. Панель же
брала «вложено» из настроек, а «стало» из состояния, и разницу показывала как
результат.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import dashboard  # noqa: E402


class Broker:
    """Брокер, у которого эксперимент начат с ДРУГОЙ суммы, чем в настройках."""

    def __init__(self, started):
        self._started = started

    def start_balance(self, name):
        return self._started.get(name, 0.0)


class TestInvestedComesFromTheExperiment:

    def _run(self, monkeypatch, settings, started, equities):
        monkeypatch.setattr(config, 'PAPER_START_BALANCES', settings)
        monkeypatch.setattr(dashboard, '_broker', Broker(started))
        monkeypatch.setattr(dashboard, '_STRATEGY_CACHE',
                            {k: {'equity': v} for k, v in equities.items()})
        rows = dashboard._directions()
        exchange = next(r for r in rows if r['id'] == 'exchange')
        return {s['name']: s for s in exchange['strategies']}, exchange

    def test_настройки_не_подменяют_начальный_депозит(self, monkeypatch):
        got, _ = self._run(monkeypatch,
                           settings={'FIBO': 20000.0},
                           started={'FIBO': 10000.0},
                           equities={'FIBO': 10111.86})
        assert got['FIBO']['invested'] == 10000.0, 'вложено — то, с чего начали'
        assert got['FIBO']['pnl'] == 111.86, 'итог совпадает с журналом сделок'

    def test_знак_итога_не_переворачивается(self, monkeypatch):
        """SMC показывался в плюс на три тысячи, торгуя в минус."""
        got, _ = self._run(monkeypatch,
                           settings={'SMC': 6800.0},
                           started={'SMC': 10000.0},
                           equities={'SMC': 9763.92})
        assert got['SMC']['pnl'] < 0
        assert round(got['SMC']['pnl'], 2) == -236.08

    def test_итог_направления_складывается_из_того_же(self, monkeypatch):
        got, exchange = self._run(monkeypatch,
                                  settings={'FIBO': 20000.0, 'SMC': 6800.0},
                                  started={'FIBO': 10000.0, 'SMC': 10000.0},
                                  equities={'FIBO': 10111.86, 'SMC': 9763.92})
        assert exchange['invested'] == 20000.0
        assert round(exchange['pnl'], 2) == round(
            sum(s['pnl'] for s in got.values()), 2)

    def test_без_брокера_остаются_настройки(self, monkeypatch):
        """Брокера ещё нет — показываем то единственное, что известно."""
        monkeypatch.setattr(config, 'PAPER_START_BALANCES', {'FIBO': 5000.0})
        monkeypatch.setattr(dashboard, '_broker', None)
        monkeypatch.setattr(dashboard, '_trade_manager', None)
        monkeypatch.setattr(dashboard, '_STRATEGY_CACHE', {})
        rows = dashboard._directions()
        exchange = next(r for r in rows if r['id'] == 'exchange')
        assert exchange['strategies'][0]['invested'] == 5000.0

    def test_сломанный_брокер_не_роняет_сводку(self, monkeypatch):
        class Broken:
            def start_balance(self, name):
                raise RuntimeError('состояние не читается')

        monkeypatch.setattr(config, 'PAPER_START_BALANCES', {'FIBO': 5000.0})
        monkeypatch.setattr(dashboard, '_broker', Broken())
        monkeypatch.setattr(dashboard, '_STRATEGY_CACHE', {})
        rows = dashboard._directions()
        exchange = next(r for r in rows if r['id'] == 'exchange')
        assert exchange['strategies'][0]['invested'] == 5000.0
