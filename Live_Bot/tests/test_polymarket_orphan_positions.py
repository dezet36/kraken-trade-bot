"""
Позиция без котировки — деньги, которые никто не пытается вернуть.

ЗАМЕР ПРЯМО В РАБОТЕ: десять открытых позиций, ВОСЕМЬ из них никто не котирует,
$12.13 заморожено — треть счёта.

Причина в том, что при запуске рабочий список берётся из СВЕЖЕГО отбора. Отбор
смотрит, где выгодно вставать сейчас, и знать не знает, где мы стояли вчера. А
позиция живёт до закрытия, и закрыть её можно только котируя: наклон против
запаса работает лишь пока мы в рынке.

Пересмотр списка (rotate) эту дыру не закрывает: он бережёт позиции в уже
имеющемся списке, но вернуть в него рынок, которого там нет, не может.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import mm, params  # noqa: E402


class Maker:
    def __init__(self, books):
        self.state = {'books': books}


class TestPositionsComeBackIntoTheWorkingList:

    def test_a_held_market_is_added(self, monkeypatch):
        monkeypatch.setattr(mm, 'known_markets', lambda: {
            'HELD': {'question': 'забытый рынок', 'tick': 0.01,
                     'token_no': 'HELD_NO', 'condition_id': 'C'}})
        maker = Maker({'HELD': {'position': 5.0}})
        got = mm.with_open_positions([{'token_id': 'FRESH'}], maker)
        tokens = [m['token_id'] for m in got]
        assert tokens == ['FRESH', 'HELD']
        added = got[1]
        assert added['question'] == 'забытый рынок'
        assert added['tick'] == 0.01
        assert added['token_no'] == 'HELD_NO', 'без встречного токена не продать'

    def test_a_market_already_in_the_list_is_not_doubled(self, monkeypatch):
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'FRESH': {'position': 5.0}})
        got = mm.with_open_positions([{'token_id': 'FRESH'}], maker)
        assert len(got) == 1, 'котировать один рынок дважды нельзя'

    def test_a_closed_position_is_not_dragged_back(self, monkeypatch):
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'GONE': {'position': 0.0}})
        got = mm.with_open_positions([{'token_id': 'FRESH'}], maker)
        assert len(got) == 1

    def test_an_unknown_market_is_still_quotable(self, monkeypatch):
        """
        Рынка может не быть даже в справочнике — а закрывать позицию всё равно
        надо. Берём безопасные значения по умолчанию.
        """
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'HELD': {'position': -5.0}})
        got = mm.with_open_positions([], maker)
        assert len(got) == 1
        assert got[0]['tick'] == 0.001
        assert got[0]['size'] == params.MM_MIN_ORDER_SIZE

    def test_we_do_not_step_inside_when_only_closing(self, monkeypatch):
        """Цель здесь не заработать спред, а выйти: шаг внутрь не нужен."""
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'HELD': {'position': 5.0}})
        got = mm.with_open_positions([], maker)
        assert got[0]['step_ticks'] == 0
        assert got[0]['holding_only'] is True


class TestBothEntryPointsUseIt:

    def test_the_service_restores_them(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        body = text[text.index('def _loop('):text.index('def start(')]
        assert 'mm.with_open_positions(markets, maker)' in body
        assert body.index('with_open_positions') < body.index('mm.step(')

    def test_the_command_line_run_restores_them_too(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        body = text[text.index('def main('):]
        assert 'with_open_positions(markets, maker)' in body


class TestTheCatalogueCoversEveryCandidate:
    """
    Отбор оставляет горстку лучших, а позиция может пережить его и остаться на
    рынке, который больше никуда не проходит. Панель тогда показывает открытую
    позицию прочерком: названия взять неоткуда.

    Замерено в работе: семь позиций из тринадцати оказались без имени.
    """

    def test_scan_hands_every_candidate_to_the_catalogue(self, monkeypatch):
        from polymarket import selector

        seen = []
        pages = [[{'id': '1', 'question': 'рынок',
                   'clobTokenIds': '["T","N"]',
                   'outcomePrices': '["0.5","0.5"]',
                   'spread': 0.02, 'volume': 100000, 'orderMinSize': 5,
                   'orderPriceMinTickSize': 0.01,
                   'endDate': '2027-01-01T00:00:00Z'}], []]
        monkeypatch.setattr(selector.client, '_get',
                            lambda url: pages.pop(0) if pages else [])
        monkeypatch.setattr(selector.params, 'PAUSE', 0)
        monkeypatch.setattr(selector.book_mod, 'fetch_many', lambda t: {})
        selector.scan(budget=100, remember=seen.append)
        assert seen and seen[0][0]['question'] == 'рынок'

    def test_a_broken_catalogue_never_stops_the_scan(self, monkeypatch):
        """Справочник — удобство: его сбой не имеет права уронить отбор."""
        from polymarket import selector

        pages = [[], []]
        monkeypatch.setattr(selector.client, '_get',
                            lambda url: pages.pop(0) if pages else [])
        monkeypatch.setattr(selector.params, 'PAUSE', 0)

        def boom(rows):
            raise OSError('диск полон')

        assert selector.scan(budget=100, remember=boom) == []

    def test_select_markets_passes_it_through(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert 'remember=remember_markets' in text


class TestThePlaceholderNeverOverwritesARealName:
    """
    ТИХАЯ ПОРЧА СОБСТВЕННЫХ ДАННЫХ. Рынок с открытой позицией возвращается в
    работу через with_open_positions, и если имени для него ещё нет, туда
    ставится прочерк. Отсюда же он попадал обратно в справочник как
    «название» — и рынок терял имя НАВСЕГДА, даже когда отбор позже приносил
    настоящее.

    Замерено: пять позиций из тринадцати остались прочерками при полностью
    исправном справочнике.
    """

    def _catalogue(self, monkeypatch, tmp_path, start=None):
        import json

        from polymarket import mm

        path = tmp_path / 'markets.json'
        if start is not None:
            path.write_text(json.dumps(start), encoding='utf-8')
        monkeypatch.setattr(mm, 'CATALOGUE', str(path))
        return mm

    def test_a_dash_does_not_replace_a_known_name(self, monkeypatch, tmp_path):
        mm = self._catalogue(monkeypatch, tmp_path,
                             {'T': {'question': 'настоящее имя'}})
        mm.remember_markets([{'token_id': 'T', 'question': '—'}])
        assert mm.known_markets()['T']['question'] == 'настоящее имя'

    def test_an_empty_name_does_not_replace_it_either(self, monkeypatch, tmp_path):
        mm = self._catalogue(monkeypatch, tmp_path,
                             {'T': {'question': 'настоящее имя'}})
        mm.remember_markets([{'token_id': 'T', 'question': None}])
        assert mm.known_markets()['T']['question'] == 'настоящее имя'

    def test_a_real_name_still_replaces_a_dash(self, monkeypatch, tmp_path):
        """Обратный порядок обязан работать: отбор приносит имя позже."""
        mm = self._catalogue(monkeypatch, tmp_path, {'T': {'question': None}})
        mm.remember_markets([{'token_id': 'T', 'question': 'нашлось'}])
        assert mm.known_markets()['T']['question'] == 'нашлось'

    def test_an_unknown_market_is_still_recorded(self, monkeypatch, tmp_path):
        """Без имени, но с токеном и тиком — этого хватает, чтобы котировать."""
        mm = self._catalogue(monkeypatch, tmp_path, {})
        mm.remember_markets([{'token_id': 'T', 'question': '—', 'tick': 0.01}])
        assert 'T' in mm.known_markets()
        assert mm.known_markets()['T']['question'] is None


class TestNamesAreLearnedFromTheExchange:
    """
    Отбор видит только активные рынки, проходящие пороги по обороту, цене и
    сроку до разрешения. А позиция переживает любые пороги: рынок затих, оборот
    упал — и мы держим в нём деньги, не зная даже названия.

    Замерено: пять позиций из тринадцати. Все пять нашлись у биржи по токену с
    первого запроса.
    """

    def test_an_unknown_token_is_asked_about(self, monkeypatch, tmp_path):
        import json

        from polymarket import mm

        path = tmp_path / 'markets.json'
        path.write_text(json.dumps({}), encoding='utf-8')
        monkeypatch.setattr(mm, 'CATALOGUE', str(path))
        monkeypatch.setattr(mm.client, '_get', lambda url: [{
            'question': 'забытый рынок', 'conditionId': 'C',
            'clobTokenIds': '["T","N"]', 'orderPriceMinTickSize': 0.01}])
        got = mm.learn_missing_names(['T'])
        assert got['T']['question'] == 'забытый рынок'
        assert got['T']['token_no'] == 'N', 'встречный токен нужен, чтобы продать'
        assert mm.known_markets()['T']['question'] == 'забытый рынок'

    def test_a_known_token_is_not_asked_about(self, monkeypatch, tmp_path):
        import json

        from polymarket import mm

        path = tmp_path / 'markets.json'
        path.write_text(json.dumps({'T': {'question': 'уже знаем'}}),
                        encoding='utf-8')
        monkeypatch.setattr(mm, 'CATALOGUE', str(path))
        asked = []
        monkeypatch.setattr(mm.client, '_get',
                            lambda url: asked.append(url) or [])
        mm.learn_missing_names(['T'])
        assert asked == [], 'лишний запрос стоит времени такта'

    def test_a_silent_exchange_does_not_break_anything(self, monkeypatch, tmp_path):
        import json

        from polymarket import mm

        path = tmp_path / 'markets.json'
        path.write_text(json.dumps({}), encoding='utf-8')
        monkeypatch.setattr(mm, 'CATALOGUE', str(path))

        def boom(url):
            raise OSError('сеть закрыта')

        monkeypatch.setattr(mm.client, '_get', boom)
        assert mm.learn_missing_names(['T']) == {}

    def test_a_dash_in_the_catalogue_is_not_a_name(self, monkeypatch, tmp_path):
        """
        Прочерк попадал в справочник из прежней версии, и проверка «имя уже
        есть» на нём срабатывала: запрос не делался, позиция оставалась
        безымянной навсегда.
        """
        import json

        from polymarket import mm

        path = tmp_path / 'markets.json'
        path.write_text(json.dumps({'T': {'question': '—'}}), encoding='utf-8')
        monkeypatch.setattr(mm, 'CATALOGUE', str(path))
        monkeypatch.setattr(mm.client, '_get', lambda url: [{
            'question': 'нашлось', 'conditionId': 'C',
            'clobTokenIds': '["T","N"]', 'orderPriceMinTickSize': 0.01}])
        assert mm.learn_missing_names(['T'])['T']['question'] == 'нашлось'

    def test_positions_outside_the_list_trigger_the_lookup(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('def with_open_positions(')
        assert 'learn_missing_names(held)' in text[spot:spot + 3000]


class TestBothTokensOfAMarketAreIndexed:
    """
    Ключом справочника был только токен «ДА», а встречный лежал внутри записи —
    искать по нему было нечего.

    Наши продажи уходят покупкой встречного токена, поэтому в журнале
    исполнений и в статистике по рынкам стояли прочерки вместо названий: рынок
    тот же, а токен другой. Замерено в панели: одиннадцать прочерков при
    полностью заполненном справочнике.
    """

    def _cat(self, monkeypatch, tmp_path, start=None):
        import json

        from polymarket import mm

        path = tmp_path / 'markets.json'
        path.write_text(json.dumps(start or {}), encoding='utf-8')
        monkeypatch.setattr(mm, 'CATALOGUE', str(path))
        return mm

    def test_the_counter_token_gets_its_own_entry(self, monkeypatch, tmp_path):
        mm = self._cat(monkeypatch, tmp_path)
        mm.remember_markets([{'token_id': 'YES', 'token_no': 'NO',
                              'question': 'вопрос', 'tick': 0.01,
                              'condition_id': 'C'}])
        known = mm.known_markets()
        assert known['YES']['question'] == 'вопрос'
        assert known['NO']['question'] == 'вопрос', 'рынок тот же'

    def test_each_side_points_at_the_other(self, monkeypatch, tmp_path):
        """Чтобы продать, нужен встречный токен — с любой стороны."""
        mm = self._cat(monkeypatch, tmp_path)
        mm.remember_markets([{'token_id': 'YES', 'token_no': 'NO',
                              'question': 'вопрос', 'tick': 0.01}])
        known = mm.known_markets()
        assert known['YES']['token_no'] == 'NO'
        assert known['NO']['token_no'] == 'YES'

    def test_a_market_without_a_twin_is_still_recorded(self, monkeypatch, tmp_path):
        mm = self._cat(monkeypatch, tmp_path)
        mm.remember_markets([{'token_id': 'YES', 'question': 'вопрос'}])
        assert mm.known_markets()['YES']['question'] == 'вопрос'

    def test_a_nameless_entry_does_not_erase_the_twin(self, monkeypatch, tmp_path):
        mm = self._cat(monkeypatch, tmp_path,
                       {'NO': {'question': 'настоящее имя'}})
        mm.remember_markets([{'token_id': 'YES', 'token_no': 'NO',
                              'question': None}])
        assert mm.known_markets()['NO']['question'] == 'настоящее имя'
