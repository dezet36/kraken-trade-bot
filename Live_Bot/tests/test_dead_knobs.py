"""
Настройка, обещающая выбор, обязана этот выбор давать.

ОТКУДА ЭТО. Разбор шести сделок RSIBB с сервера. В коде стоит развилка с
честным намерением:

    'widen' — расширить стоп, приняв худшее отношение;
    'skip'  — не брать сетап вовсе.
    Что верно — решает замер, поэтому выбор вынесен в параметр.

Замер невозможен. Расширение до пола даёт rr = half / пол, а расширяем мы
ровно тогда, когда half < пол — значит расширенное отношение ВСЕГДА меньше
единицы. Следом стоит `rr < min_rr`, и при MIN_RR = 1.0 (значение по
умолчанию) отклоняется каждый расширенный сетап. 'widen' заканчивается тем же
отказом, что и 'skip'.

Проверено перебором: при MIN_RR 1.0, 0.8, 0.7 — отказ; развилка оживает с
0.625 и ниже.

ПОВЕДЕНИЕ ВЕРНОЕ, ОБЕЩАНИЕ ЛОЖНОЕ. Брать сделку с отношением ниже единицы не
стоит, и код прав. Неверно было утверждать, что выбор есть: человек поставит
'skip', замерит, получит те же числа и запишет «разницы нет» — вывод
буквально верный и обманчивый по сути.

ЭТО НЕ ЕДИНИЧНЫЙ СЛУЧАЙ. Того же рода зона B у FIBO: имя зоны стояло
литералом 'Zone_A', и статистика по зоне B не наполнялась никогда
(исправлено в v1.3.6). Поэтому проверка в диагностике общая, а не про один
параметр.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rsibb import core, params                             # noqa: E402


def _thin_setup(half=0.25, entry=100.0):
    """Канал заведомо тоньше пола: пол 0.4% от цены."""
    return {'direction': 'LONG', 'half_width': half, 'band': entry,
            'close': entry, 'entry_mode': 'touch'}


class TestTheChoiceIsHonestlyDescribed:

    def test_widen_and_skip_agree_at_the_default_threshold(self):
        """РОВНО ТОТ ДЕФЕКТ: развилка есть, разницы нет."""
        s = _thin_setup()
        assert core.build_trade(dict(s), thin_stop='widen') is None
        assert core.build_trade(dict(s), thin_stop='skip') is None

    def test_they_differ_once_the_threshold_allows_it(self):
        s = _thin_setup()
        got = core.build_trade(dict(s), thin_stop='widen', min_rr=0.6)
        assert got is not None and got['rr'] < 1.0
        assert core.build_trade(dict(s), thin_stop='skip', min_rr=0.6) is None

    def test_a_wide_channel_is_untouched_by_the_choice(self):
        """Пол не упирается — обе ветки дают одну и ту же сделку, и это верно."""
        s = _thin_setup(half=0.8)
        w = core.build_trade(dict(s), thin_stop='widen')
        k = core.build_trade(dict(s), thin_stop='skip')
        assert w and k and w['stop'] == k['stop'] and w['rr'] == k['rr']


class TestTheInertnessIsNamed:

    def test_the_default_threshold_makes_it_inert(self):
        assert core.widen_is_inert(1.0)
        assert core.widen_is_inert()          # то же значение по умолчанию

    def test_a_lower_threshold_revives_it(self):
        assert not core.widen_is_inert(0.6)
        assert not core.widen_is_inert(0.625)

    def test_the_boundary_is_one(self):
        """
        Расширенное отношение строго меньше единицы, поэтому порог ровно 1.0
        уже отклоняет всё.
        """
        assert core.widen_is_inert(1.0)
        assert not core.widen_is_inert(0.999)

    def test_nonsense_does_not_crash(self):
        for bad in ('нет', None if False else 'x', [], {}):
            assert core.widen_is_inert(bad) is False

    def test_the_claim_matches_the_code(self):
        """
        Утверждение «неотличим» проверяется не на слово: при каждом пороге,
        который widen_is_inert зовёт мёртвым, обе ветки обязаны совпасть.
        """
        s = _thin_setup()
        for mr in (1.0, 1.2, 2.0):
            assert core.widen_is_inert(mr)
            assert core.build_trade(dict(s), thin_stop='widen', min_rr=mr) is None
            assert core.build_trade(dict(s), thin_stop='skip', min_rr=mr) is None

    def test_and_the_reverse_claim_too(self):
        s = _thin_setup()
        for mr in (0.6, 0.5, 0.3):
            assert not core.widen_is_inert(mr)
            assert core.build_trade(dict(s), thin_stop='widen', min_rr=mr) is not None


class TestTheCommentStoppedPromising:

    SRC = open(os.path.join(ROOT, 'rsibb', 'core.py'), encoding='utf-8').read()

    def test_the_condition_is_written_down(self):
        assert 'MIN_RR >= 1.0' in self.SRC or 'MIN_RR = 1.0' in self.SRC
        assert 'неотличим' in self.SRC

    def test_the_bare_promise_is_gone(self):
        """
        Стояло «Что верно — решает замер, поэтому выбор вынесен в параметр».
        Замысел верный, но как есть эта фраза неправдива: при пороге по
        умолчанию замер сравнил бы значение само с собой. Замысел сохранён в
        новом объяснении, обещание — снято.
        """
        assert 'Что верно — решает замер, поэтому выбор вынесен' not in self.SRC
        assert 'ВЫБОР СУЩЕСТВУЕТ НЕ ВСЕГДА' in self.SRC
        assert 'сравнил бы значение само с собой' in self.SRC


class TestDiagnosticsShowsIt:

    def test_it_is_registered(self):
        import doctor
        assert any(fn is doctor.check_strategy_knobs for _, fn in doctor.CHECKS)

    def test_it_warns_at_the_current_settings(self, monkeypatch):
        import doctor
        monkeypatch.setattr(params, 'THIN_STOP', 'widen')
        monkeypatch.setattr(params, 'MIN_RR', 1.0)
        r = doctor.check_strategy_knobs()
        assert r['level'] == 'warn' and 'THIN_STOP' in r['detail']

    def test_a_working_knob_is_silent(self, monkeypatch):
        import doctor
        monkeypatch.setattr(params, 'THIN_STOP', 'skip')
        assert doctor.check_strategy_knobs()['level'] == 'ok'

    def test_a_lower_threshold_also_clears_it(self, monkeypatch):
        import doctor
        monkeypatch.setattr(params, 'THIN_STOP', 'widen')
        monkeypatch.setattr(params, 'MIN_RR', 0.6)
        assert doctor.check_strategy_knobs()['level'] == 'ok'

    def test_it_never_fails_the_run(self, monkeypatch):
        """
        Мёртвая настройка — повод посмотреть, а не отказ работать: торговле
        она не мешает, она мешает делать выводы.
        """
        import doctor
        monkeypatch.setattr(params, 'THIN_STOP', 'widen')
        monkeypatch.setattr(params, 'MIN_RR', 1.0)
        assert doctor.check_strategy_knobs()['level'] != 'fail'
