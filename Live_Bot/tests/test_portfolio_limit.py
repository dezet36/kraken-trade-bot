"""
Проверка предела на весь портфель.

Зачем он вообще нужен: каждая стратегия соблюдает СВОЙ лимит слотов. Три
стратегии по шесть позиций при риске 0.5% выводят под риск 9% депозита
одновременно — и ни одна из них своих правил не нарушила. Предел смотрит
поверх всех, поэтому проверяется он тоже поверх: на брокере с несколькими
стратегиями сразу.

Отдельно защищается выключенное состояние. Предел по умолчанию отключён, и
ошибка, при которой он начнёт срабатывать сам по себе, проявится как
«бот перестал открывать сделки» без единой записи о причине.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TRADING_MODE', 'PAPER')
    for module in ('config', 'settings_store'):
        sys.modules.pop(module, None)
    import settings_store
    return settings_store


class TestНастройка:
    def test_po_umolchaniyu_predel_vyklyuchen(self, store):
        assert store.portfolio_risk_pct() == 0
        assert store.portfolio_max_positions() == 0

    def test_ustanavlivaetsya_i_chitaetsya(self, store):
        store.save({store.PORTFOLIO: {'portfolio_risk_pct': 5,
                                      'portfolio_max_positions': 8}})
        assert store.portfolio_risk_pct() == 5
        assert store.portfolio_max_positions() == 8

    def test_otklyuchaetsya_nulyom(self, store):
        store.save({store.PORTFOLIO: {'portfolio_risk_pct': 5}})
        assert store.portfolio_risk_pct() == 5
        store.save({store.PORTFOLIO: {'portfolio_risk_pct': 0}})
        assert store.portfolio_risk_pct() == 0

    def test_znachenie_vne_diapazona_ne_prinimaetsya(self, store):
        """
        Опечатка в поле «предел» — это не косметика: 500 вместо 50 снимает
        ограничение целиком, и узнать об этом можно только по убытку.
        """
        store.save({store.PORTFOLIO: {'portfolio_risk_pct': 500}})
        assert store.portfolio_risk_pct() <= 100

    def test_perezhivaet_perechitivanie(self, store):
        store.save({store.PORTFOLIO: {'portfolio_risk_pct': 7.5}})
        store.load(force=True)
        assert store.portfolio_risk_pct() == 7.5

    def test_nastroyki_strategiy_ne_zatronuty(self, store):
        before = store.risk_pct('SMC')
        store.save({store.PORTFOLIO: {'portfolio_risk_pct': 5}})
        assert store.risk_pct('SMC') == before


class TestПрименение:
    """
    Считаем арифметику предела напрямую: она решает, откроется сделка или
    нет, и ошибка здесь либо остановит торговлю, либо снимет защиту.
    """

    @staticmethod
    def room(used, deposit, add_pct, limit):
        """Повторяет расчёт из _portfolio_room."""
        if not limit or deposit <= 0:
            return True
        after = (used + deposit * add_pct / 100) / deposit * 100
        return after <= limit

    def test_propuskaet_poka_est_zapas(self):
        # под риском 2%, сделка добавит 0.5%, предел 5%
        assert self.room(used=200, deposit=10_000, add_pct=0.5, limit=5)

    def test_ne_propuskaet_za_predelom(self):
        # под риском 4.8%, сделка добавит 0.5% -> 5.3% при пределе 5%
        assert not self.room(used=480, deposit=10_000, add_pct=0.5, limit=5)

    def test_granica_vklyuchitelno(self):
        # ровно 5.0% при пределе 5% — проходит
        assert self.room(used=450, deposit=10_000, add_pct=0.5, limit=5)

    def test_vyklyuchennyi_predel_propuskaet_vsyo(self):
        assert self.room(used=9_000, deposit=10_000, add_pct=5, limit=0)

    def test_nulevoy_depozit_ne_delit_na_nol(self):
        assert self.room(used=0, deposit=0, add_pct=0.5, limit=5)


class TestУчёт:
    def test_ozhidayushchie_ordera_schitayutsya(self, store, monkeypatch, tmp_path):
        """
        Ордер, который вот-вот нальётся, — уже принятый риск. Не считать его
        значило бы обходить собственный предел: бот набрал бы полный лимит
        ордерами, а потом они превратились бы в позиции все разом.
        """
        import paper_broker

        broker = paper_broker.PaperBroker.__new__(paper_broker.PaperBroker)
        broker.strategies = ('FIBO', 'SMC')
        broker.balance = lambda s: 10_000.0
        broker.positions = lambda s: (
            {'BTCUSDT': {'risk_amount': 50.0}} if s == 'FIBO' else {})
        broker.pending = lambda s: (
            {'ETHUSDT': {'risk_amount': 50.0}} if s == 'SMC' else {})

        amount, pct, deposit = broker.portfolio_risk()
        assert amount == 100.0            # позиция + ордер
        assert deposit == 20_000.0        # два депозита
        assert pct == pytest.approx(0.5)
        assert broker.portfolio_slots() == 2
