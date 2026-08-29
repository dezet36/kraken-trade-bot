"""
Договор о форме сигнала: что обязана вернуть ЛЮБАЯ стратегия.

ЗАЧЕМ ЭТОТ ФАЙЛ. Сигнал стратегии читают шесть разных мест — сборка
контекста сделки, журнал, исполнитель, дашборд, — и каждое берёт поля
напрямую, по имени. Договор при этом нигде не записан: он существовал в
голове у того, кто писал первую стратегию.

Стратегия уровней его нарушала: у неё не было поля trigger. Не ломалось это
ровно потому, что до тех мест она НЕ ДОХОДИЛА НИ РАЗУ — диспетчер в bot.py
отдавал её кандидатов ветке Фибоначчи. Стоило починить диспетчер, как
открылась дорога к падению на первом же входе: KeyError('trigger') вместо
сделки. Два дефекта прикрывали друг друга, и по отдельности каждый выглядел
как «всё работает».

Поэтому договор теперь записан здесь, проверкой, и распространяется на все
стратегии сразу.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Поля, которые читаются напрямую и без которых что-нибудь упадёт.
REQUIRED = ('trading_pair', 'setup', 'params', 'trigger')
REQUIRED_SETUP = ('type',)
REQUIRED_PARAMS = ('entry', 'stop_loss', 'take_profit_1')
REQUIRED_TRIGGER = ('zone',)


def _levels_signal(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    import strategy_levels

    stamps = pd.date_range('2026-08-01', periods=60, freq='h', tz='UTC')
    df = pd.DataFrame({'timestamp': stamps, 'close': np.full(60, 100.0)})
    setup = {
        'direction': 'SHORT', 'level': 100.0, 'touches': 3, 'mirror': False,
        'entry': 99.5, 'stop_loss': 100.6, 'target': 97.0, 'rr': 2.3,
        'sl_distance': 1.1, 'volume_ratio': 2.0, 'reclaim_index': 50,
        'pierce_index': 48, 'pierce_extreme': 100.8,
        'points': [{'index': 4, 'price': 100.2, 'kind': 'high'}],
        'first_index': 4,
    }
    return strategy_levels._to_bot_signal(setup, 'BTCUSDT', 10_000, df)


def test_levels_signal_matches_contract(tmp_path, monkeypatch):
    signal = _levels_signal(tmp_path, monkeypatch)
    for field in REQUIRED:
        assert field in signal, f'нет обязательного поля {field}'
    for field in REQUIRED_SETUP:
        assert field in signal['setup'], f'нет setup.{field}'
    for field in REQUIRED_PARAMS:
        assert field in signal['params'], f'нет params.{field}'
    for field in REQUIRED_TRIGGER:
        assert field in signal['trigger'], f'нет trigger.{field}'


def test_levels_signal_builds_context(tmp_path, monkeypatch):
    """
    Контекст сделки собирается — именно здесь и падало.

    Это не дубль предыдущей проверки: список полей может разойтись с тем, что
    код на самом деле читает, а вызов настоящей функции — не может.
    """
    signal = _levels_signal(tmp_path, monkeypatch)
    signal['strategy'] = 'LEVELS'
    import paper_broker as pb

    ctx = pb.PaperBroker._context('LEVELS', signal)
    assert ctx['zone'] == 'LEVEL'
    assert ctx.get('why')
    # Разметка сделки едет вместе с контекстом — без неё нет графика.
    assert 'geometry' in ctx


def test_levels_trade_opens_end_to_end(tmp_path, monkeypatch):
    """
    Сделка от уровня действительно открывается брокером.

    Самая честная проверка из трёх: она проходит весь путь, а не отдельные
    его куски, и поймала бы оба дефекта сразу.
    """
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    signal = _levels_signal(tmp_path, monkeypatch)
    signal['strategy'] = 'LEVELS'

    import paper_broker as pb
    # Предел расхода на вход выключен намеренно: в приборе стоп 1.1%, а это
    # 6.2% риска в комиссиях, и обычный вход с такими числами не проходит.
    # Проверка эта про ФОРМУ сигнала — доходит ли он от стратегии до брокера
    # целым, — а не про то, по карману ли он. Настоящие уровни держатся
    # заметно шире: по журналу сервера медиана 3.4%.
    #
    # Гасим через pb.config, а не через свежий `import config`: набор
    # перезагружает модули между проверками, и патч на другом экземпляре
    # брокеру не виден — так и вышло с первой попытки.
    monkeypatch.setattr(pb.config, 'MAX_ENTRY_COST_SHARE_PCT', 0)

    broker = pb.PaperBroker(client=None, strategies=('LEVELS',))
    assert broker.open('LEVELS', signal), 'брокер не принял сигнал уровней'

    book = broker.pending('LEVELS') or {}
    positions = broker.positions('LEVELS') or {}
    assert book or positions, 'ни ордера, ни позиции не появилось'

    record = (list(book.values()) + list(positions.values()))[0]
    context = record.get('context') or {}
    geometry = context.get('geometry') or {}
    # Разметка должна быть уровневой, а не фибовской: пустая означала бы, что
    # сигнал снова собрали не той веткой.
    assert any('уровень' in (line.get('label') or '')
               for line in geometry.get('lines', [])), \
        'в разметке нет уровня — сигнал собран чужой стратегией'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
