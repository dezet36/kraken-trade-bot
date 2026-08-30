"""
Журнал отвечает на вопросы, на которые раньше отвечать было нечем.

ОТКУДА ЭТО. Разбор 364 сделок 29 августа 2026 упёрся в четыре стены подряд.

ПЕРВАЯ. 21 сделка доходила до цели и закрылась в ноль по безубытку. Что было
раньше — рост или просадка — восстановить не удалось: журнал знал, НАСКОЛЬКО
цена уходила, и не знал, КОГДА. Вопрос «безубыток спасает или режет» остался
догадкой. Теперь есть mfe_min и mae_min.

ВТОРАЯ. Что цена делала ПОСЛЕ выхода, не знал никто. А это и решает судьбу
стопов и целей: выбило по стопу — дошла бы до цели? взяли цель — прошла бы
дальше? Теперь есть follow_up.csv.

ТРЕТЬЯ. Отказы бота нигде не оставались: воронка отсева живёт в памяти до
следующего цикла. Стало срочным, когда появился предел издержек — он отсекает
17% сетапов у фибо и 80% у SMC, и проверить, правильно ли, было нечем. Фильтр,
чьё действие нельзя измерить, — то самое, за что уже досталось зоне B и
развилке THIN_STOP. Теперь есть refused.csv.

ЧЕТВЁРТАЯ. Обстановка на входе не записывалась. Лонги дали +0.20R, шорты
-0.33R, и объяснить это удалось только прикидкой, что биткоин за месяц вырос
на 8.3%. Теперь есть atr_pct и hour_utc.
"""

import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import follow_up                                          # noqa: E402
import refused                                            # noqa: E402

SRC = open(os.path.join(ROOT, 'paper_broker.py'), encoding='utf-8').read()


def _method(name):
    spot = SRC.index('def ' + name)
    return SRC[spot:SRC.index('\n    def ', spot + 10)]


class TestTheJournalKnowsWhenNotJustHowFar:

    def test_the_columns_exist(self):
        import paper_broker
        for name in ('mfe_min', 'mae_min'):
            assert name in paper_broker.COLUMNS, name

    def test_they_stand_next_to_the_distances(self):
        import paper_broker
        c = paper_broker.COLUMNS
        assert abs(c.index('mfe_min') - c.index('mfe_r')) <= 2

    def test_the_moment_is_remembered_with_the_price(self):
        """
        Записать цену и не записать момент — вернуться туда, откуда шли:
        насколько ушла, известно, а когда — нет.
        """
        body = _method('_process_position')
        assert "pos['mfe_ts']" in body and "pos['mae_ts']" in body

    def test_the_moment_moves_only_when_the_price_does(self):
        """
        Обновлять момент на каждой свече значило бы записать время последней
        свечи, а не время пика.
        """
        body = _method('_process_position')
        assert "if best != pos['mfe_price']" in body
        assert "if worst != pos['mae_price']" in body


class TestTheEntryContextIsRecorded:

    def test_the_columns_exist(self):
        import paper_broker
        for name in ('atr_pct', 'hour_utc'):
            assert name in paper_broker.COLUMNS, name

    def _broker(self, tmp_path, monkeypatch):
        monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
        monkeypatch.setenv('PAPER_FUNDING', 'false')
        for name in ('config', 'paper_broker'):
            sys.modules.pop(name, None)
        import paper_broker
        return paper_broker.PaperBroker(client=None, strategies=('FIBO',))

    def test_volatility_needs_history_before_it_speaks(self, tmp_path, monkeypatch):
        """Размах по одной свече — не размах. Лучше пусто, чем выдумка."""
        b = self._broker(tmp_path, monkeypatch)
        assert b._atr_pct('BTCUSDT', 100.0) == ''
        b._track_volatility('BTCUSDT', 1_000_000, 101.0, 99.0, 100.0)
        assert b._atr_pct('BTCUSDT', 100.0) == ''

    def test_volatility_speaks_once_it_has_enough(self, tmp_path, monkeypatch):
        b = self._broker(tmp_path, monkeypatch)
        for i in range(10):
            b._track_volatility('BTCUSDT', 1_000_000 + i * 300_000, 101.0, 99.0, 100.0)
        assert abs(b._atr_pct('BTCUSDT', 100.0) - 2.0) < 0.01

    def test_the_same_candle_is_counted_once(self, tmp_path, monkeypatch):
        """
        Одну пару обходят несколько стратегий подряд. Без защиты по времени
        размах считался бы столько раз, сколько стратегий включено.
        """
        b = self._broker(tmp_path, monkeypatch)
        for i in range(6):
            b._track_volatility('BTCUSDT', 1_000_000 + i * 300_000, 101.0, 99.0, 100.0)
        was = b.state['atr']['BTCUSDT']['n']
        b._track_volatility('BTCUSDT', 1_000_000 + 5 * 300_000, 150.0, 50.0, 100.0)
        assert b.state['atr']['BTCUSDT']['n'] == was


class TestWhatHappenedAfterTheExit:

    def _watch(self, direction='LONG', entry=100.0, stop=96.0, tp1=108.0):
        pos = {'strategy': 'FIBO', 'pair': 'BTCUSDT', 'direction': direction,
               'entry_price': entry, 'stop_loss': stop, 'targets': [tp1],
               'closed_at': '2026-08-29T22:00:00+00:00'}
        return follow_up.watch(pos, 1_000_000_000, 104.0, 'TP1', 7)

    def test_the_watch_keeps_what_it_needs(self):
        """Позиция вот-вот исчезнет — всё нужное берётся сразу."""
        w = self._watch()
        assert w['trade_id'] == 7 and w['sl_dist'] == 4.0
        assert w['entry_price'] == 100.0 and w['tp1'] == 108.0

    def test_price_after_exit_is_measured_from_the_entry(self):
        """
        Вопрос звучит «до какого R дошла бы сделка», а не «куда ушла цена».
        Значит считать надо от входа, а не от выхода.
        """
        w = self._watch()
        follow_up.advance([w], 'BTCUSDT', 1_000_000_000 + 300_000, 112.0, 110.0, 111.0)
        assert w['best'] == 112.0
        assert follow_up._r(w, 112.0) == 3.0        # (112-100)/4

    def test_a_short_counts_the_other_way(self):
        w = self._watch(direction='SHORT', entry=100.0, stop=104.0, tp1=92.0)
        follow_up.advance([w], 'BTCUSDT', 1_000_000_000 + 300_000, 90.0, 88.0, 89.0)
        assert w['best'] == 88.0
        assert follow_up._r(w, 88.0) == 3.0         # (100-88)/4

    def test_reaching_the_target_after_the_exit_is_noted(self):
        """РАДИ ЭТОГО ВСЁ: стоп выбил, а цель потом взяли бы."""
        w = self._watch()
        follow_up.advance([w], 'BTCUSDT', 1_000_000_000 + 300_000, 109.0, 107.0, 108.5)
        assert w['hit_tp1'] == 1

    def test_the_horizons_fill_in_order(self):
        w = self._watch()
        base = 1_000_000_000
        follow_up.advance([w], 'BTCUSDT', base + 3_600_000, 105.0, 104.0, 104.5)
        assert w['marks'].get('1') == 104.5 and '4' not in w['marks']
        follow_up.advance([w], 'BTCUSDT', base + 4 * 3_600_000, 106.0, 105.0, 105.5)
        assert w['marks'].get('4') == 105.5

    def test_the_watch_finishes_at_the_last_horizon(self):
        w = self._watch()
        base = 1_000_000_000
        done = follow_up.advance([w], 'BTCUSDT', base + 3_600_000, 105.0, 104.0, 104.5)
        assert done == []
        done = follow_up.advance([w], 'BTCUSDT', base + 13 * 3_600_000, 105.0, 104.0, 104.5)
        assert done == [w]

    def test_another_pair_is_not_touched(self):
        w = self._watch()
        follow_up.advance([w], 'ETHUSDT', 1_000_000_000 + 300_000, 999.0, 998.0, 999.0)
        assert w['best'] == 100.0

    def test_candles_before_the_close_are_ignored(self):
        """Наблюдение начинается ПОСЛЕ выхода, иначе оно повторит саму сделку."""
        w = self._watch()
        follow_up.advance([w], 'BTCUSDT', 1_000_000_000 - 300_000, 130.0, 129.0, 130.0)
        assert w['best'] == 100.0

    def test_the_row_carries_the_answers(self):
        w = self._watch()
        base = 1_000_000_000
        follow_up.advance([w], 'BTCUSDT', base + 13 * 3_600_000, 112.0, 110.0, 111.0)
        r = follow_up.row(w)
        assert r['trade_id'] == 7 and r['best_after_r'] == 3.0
        assert r['hit_tp1_after'] == 1
        assert set(follow_up.COLUMNS) >= set(r)

    def test_it_is_a_separate_file(self):
        """
        Наблюдение длится часами после закрытия. Колонкой в журнале оно
        означало бы дописывание строки задним числом — а журнал сделок пишется
        только вперёд, и это его главное свойство.
        """
        assert follow_up.CSV_PATH.endswith('follow_up.csv')
        assert 'follow_up.write(' in SRC

    def test_watched_pairs_get_their_candles(self):
        """
        Свечи запрашивались только там, где висит ордер или стоит позиция.
        Закрытая сделка не имеет ни того ни другого — и наблюдение не
        получало ни одной свечи. Поймано сквозным прогоном.
        """
        spot = SRC.index('def update(self)')
        body = SRC[spot:SRC.index('\n    MAX_BARS', spot)]
        assert "self.state.get('follow')" in body


class TestRefusalsAreKept:

    def test_only_the_gates_are_written(self):
        """
        «По паре уже есть позиция» случается десятки раз за цикл. Это не отказ
        от сделки, а отсутствие сетапа, и файл распух бы, ничего не объясняя.
        """
        spot = SRC.index('уже есть фантомная позиция')
        # До ПЕРВОГО return, а не на 200 символов вперёд: следом идёт проверка
        # кулдауна, и она отказ пишет законно — там сетап был готов.
        block = SRC[spot:SRC.index('return False', spot)]
        assert '_refuse(' not in block

    def test_every_gate_is_covered(self):
        for gate in ('предел портфеля', 'направленный кэп', 'кулдаун',
                     'предел издержек'):
            assert "'" + gate + "'" in SRC, gate

    def test_the_setup_is_saved_whole(self, tmp_path, monkeypatch):
        """
        Без цены входа, стопа и цели отказ бесполезен: прогнать его потом и
        спросить «что было бы» станет нельзя.
        """
        monkeypatch.setattr(refused, 'CSV_PATH', str(tmp_path / 'refused.csv'))
        signal = {'trading_pair': 'BTCUSDT', 'setup': {'type': 'LONG'},
                  'params': {'entry': 100.0, 'stop_loss': 99.0, 'rr': 3.0,
                             'tp_targets': [103.0]}}
        refused.record('FIBO', signal, 'предел издержек', 'дорого', 6.8)
        rows = list(csv.DictReader(open(tmp_path / 'refused.csv', encoding='utf-8')))
        assert len(rows) == 1
        r = rows[0]
        assert r['entry'] == '100.0' and r['stop_loss'] == '99.0'
        assert r['tp1'] == '103.0' and r['cost_share_pct'] == '6.8'
        assert r['gate'] == 'предел издержек'

    def test_a_broken_signal_does_not_stop_trading(self, tmp_path, monkeypatch):
        """Запись наблюдений — вспомогательное дело, и падать из-за неё нельзя."""
        monkeypatch.setattr(refused, 'CSV_PATH', str(tmp_path / 'refused.csv'))
        refused.record('FIBO', None, 'кулдаун')      # не бросает

    def test_the_writer_is_wrapped_on_the_broker_side_too(self):
        spot = SRC.index('def _refuse(')
        body = SRC[spot:SRC.index('\nJOURNAL_CSV', spot)]
        assert 'except Exception' in body
