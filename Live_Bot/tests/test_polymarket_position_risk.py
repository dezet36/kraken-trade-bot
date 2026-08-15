"""
Позиция обязана иметь имя и предел убытка.

ДВА НАБЛЮДЕНИЯ С ЖИВОГО СЧЁТА, и оба стоили денег или понимания.

ПРОЧЕРК ВМЕСТО НАЗВАНИЯ. План отбора перезаписывается при каждом пересмотре, а
позиция живёт до закрытия — и рынок, где мы стоим, из плана исчезает. Замерено:
ДЕСЯТЬ позиций из десяти оказались вне текущего плана, и все десять панель
показывала прочерком. Человек видел открытую позицию без единого признака, на
каком она рынке.

ОДНА ПОЗИЦИЯ СДЕЛАЛА 60% УБЫТКА. Разбор двенадцати живых позиций: настоящий
убыток от движения рынка $1.56, из них $0.94 — рынок, переоценившийся с 0.637
до 0.450. Остальные одиннадцать вместе дали $0.62. Круг приносит три-восемь
процентов ставки; позиция, потерявшая двадцать, съедает несколько удачных
кругов, и держать её дальше значит ставить на исход.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket  # noqa: E402
from polymarket import params  # noqa: E402


class TestMarketsAreRememberedByName:

    def test_catalogue_survives_a_rescan(self, tmp_path, monkeypatch):
        from polymarket import mm

        monkeypatch.setattr(mm, 'CATALOGUE', str(tmp_path / 'known.json'))
        mm.remember_markets([{'token_id': 'A', 'question': 'первый рынок'}])
        mm.remember_markets([{'token_id': 'B', 'question': 'второй рынок'}])
        known = mm.known_markets()
        assert known['A']['question'] == 'первый рынок', 'прежний не потерялся'
        assert known['B']['question'] == 'второй рынок'

    def test_a_position_outside_the_plan_still_has_a_name(self):
        """Ровно тот случай: позиция есть, в плане рынка нет."""
        books = {'A': {'position': 5, 'orders': {
            'bid': {'price': 0.2, 'size': 5, 'ts': 0, 'queue': 0}, 'ask': None}}}
        rows = polymarket._standing_quotes(
            books, planned=[], catalogue={'A': {'question': 'забытый рынок'}})
        assert rows[0]['question'] == 'забытый рынок'

    def test_the_plan_still_wins_where_it_knows_more(self):
        """У плана есть ожидаемое время, у справочника — нет."""
        books = {'A': {'position': 0, 'orders': {
            'bid': {'price': 0.2, 'size': 5, 'ts': 0, 'queue': 0}, 'ask': None}}}
        rows = polymarket._standing_quotes(
            books,
            planned=[{'token_id': 'A', 'question': 'свежий', 'wait_hours': 0.5}],
            catalogue={'A': {'question': 'старый'}})
        assert rows[0]['question'] == 'свежий'
        assert rows[0]['expected_min'] == 30

    def test_an_unknown_token_is_still_a_dash(self):
        books = {'Z': {'position': 5, 'orders': {
            'bid': {'price': 0.2, 'size': 5, 'ts': 0, 'queue': 0}, 'ask': None}}}
        rows = polymarket._standing_quotes(books, planned=[], catalogue={})
        assert rows[0]['question'] == '—', 'врать название нельзя'


    def test_working_markets_are_remembered_too(self):
        """
        Отбор отдаёт горсть лучших, но позиция может остаться на рынке, который
        из отбора уже выпал. Пополняем справочник тем, что котируем сейчас.
        """
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('def step(')
        assert 'remember_markets(markets)' in text[spot:spot + 2500]


class TestOnePositionCannotSinkTheAccount:

    def test_the_limit_is_a_share_of_cost(self):
        assert 0 < params.MM_MAX_POSITION_LOSS < 1

    def test_step_marks_hurt_positions_for_closing(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert 'hurt = set()' in text
        assert 'params.MM_MAX_POSITION_LOSS' in text
        assert 'stale=str(token) in stale or str(token) in hurt' in text

    def test_a_long_position_counts_a_falling_price_as_loss(self):
        cost, mark = 0.637, 0.450
        loss = (mark - cost) / cost
        assert loss <= -params.MM_MAX_POSITION_LOSS, \
            'та самая позиция, что сделала 60% убытка, попадает под предел'

    def test_a_small_move_is_left_alone(self):
        cost, mark = 0.227, 0.214
        loss = (mark - cost) / cost
        assert loss > -params.MM_MAX_POSITION_LOSS, \
            'обычное дрожание не повод бросать рынок'

    def test_a_short_position_counts_a_rising_price_as_loss(self):
        cost, mark = 0.20, 0.30
        loss = (cost - mark) / cost
        assert loss <= -params.MM_MAX_POSITION_LOSS
