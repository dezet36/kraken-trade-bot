"""
Опечатка в настройках не имеет права останавливать бота.

ОТКУДА ЭТО. У каждой стратегии был свой набор `_f`/`_i`/`_s` — четыре строки,
скопированные трижды. Копии разошлись: LEVELS ловила ошибку разбора и
откатывалась на значение по умолчанию, RSIBB и SMC роняли импорт.

Цена расхождения оказалась несоразмерна его размеру. Стратегии импортируются в
bot.py на верхнем уровне, поэтому `RSIBB_RISK_PCT=0,5` — обычная опечатка в
русской раскладке — не запускало НИЧЕГО: ни одной стратегии, ни панели, ни
ведения уже открытых позиций. В собранном приложении это выглядело бы как
«запустил, и ничего не произошло».

Проверено до исправления: ValueError: could not convert string to float: '0,5'
на импорте bot.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import params_env                                           # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    return monkeypatch


class TestNumbersWrittenByHand:

    def setup_method(self):
        self.f, self.i, self.b, self.s = params_env.reader('T')

    def test_comma_is_read_as_a_decimal_point(self, env):
        """Ровно та опечатка, что валила бота целиком."""
        env.setenv('T_X', '0,5')
        assert self.f('X', 9.9) == 0.5

    def test_a_dot_still_works(self, env):
        env.setenv('T_X', '0.5')
        assert self.f('X', 9.9) == 0.5

    def test_a_comma_between_thousands_is_not_a_point(self, env):
        """
        «1,234.5» — это тысяча двести тридцать четыре с половиной. Подменив
        запятую точкой, мы прочли бы 1.234, то есть в тысячу раз меньше.
        """
        env.setenv('T_X', '1,234.5')
        assert self.f('X', 0) == 1234.5

    def test_spaces_between_thousands_are_ignored(self, env):
        env.setenv('T_X', '1 000,5')
        assert self.f('X', 0) == 1000.5

    def test_surrounding_spaces_are_trimmed(self, env):
        env.setenv('T_X', '  2.0  ')
        assert self.f('X', 0) == 2.0


class TestNonsenseFallsBackLoudly:

    def setup_method(self):
        self.f, self.i, self.b, self.s = params_env.reader('T')

    def test_letters_do_not_crash(self, env):
        env.setenv('T_X', 'абвгд')
        assert self.f('X', 0.5) == 0.5

    def test_an_empty_value_does_not_crash(self, env):
        env.setenv('T_X', '')
        assert self.f('X', 0.5) == 0.5

    def test_it_says_so(self, env, monkeypatch):
        """
        Молчаливый откат превратил бы настройку в декорацию: человек выставил
        одно, бот торгует другим, и узнать об этом неоткуда.
        """
        said = []
        monkeypatch.setattr(params_env, 'log', lambda m: said.append(m))
        env.setenv('T_X', 'мусор')
        self.f('X', 0.5)
        assert said and 'мусор' in said[0] and '0.5' in said[0]

    def test_whole_numbers_survive_a_decimal(self, env):
        """«20.0» в поле целого — не ошибка, а привычка. Читается как 20."""
        env.setenv('T_X', '20.0')
        assert self.i('X', 5) == 20


class TestDefaultsAndFlags:

    def setup_method(self):
        self.f, self.i, self.b, self.s = params_env.reader('T')

    def test_absent_means_default(self, env):
        env.delenv('T_X', raising=False)
        assert self.f('X', 1.25) == 1.25
        assert self.i('X', 7) == 7
        assert self.s('X', 'привет') == 'привет'
        assert self.b('X', True) is True

    def test_russian_yes_is_yes(self, env):
        """Флаг вправе быть написан по-русски: его пишет человек, не машина."""
        env.setenv('T_X', 'да')
        assert self.b('X', False) is True

    def test_anything_else_is_no(self, env):
        env.setenv('T_X', 'нет')
        assert self.b('X', True) is False


class TestEveryStrategyKeepsItsOwnSettings:
    """
    Префикс — это и есть требование «каждая стратегия работает отдельно по
    своим параметрам». Общее имя RISK_PCT меняло бы сразу две.
    """

    def test_prefixes_do_not_collide(self, env):
        lf = params_env.reader('LEVELS')[0]
        rf = params_env.reader('RSIBB')[0]
        env.setenv('LEVELS_RISK_PCT', '0.9')
        env.setenv('RSIBB_RISK_PCT', '0.2')
        assert lf('RISK_PCT', 0) == 0.9
        assert rf('RISK_PCT', 0) == 0.2

    def test_all_three_strategies_use_the_shared_reader(self):
        """
        Своя копия читателя — это начало нового расхождения. Именно так две
        стратегии оказались хрупкими, а третья нет.
        """
        for path in ('levels/params.py', 'rsibb/params.py', 'smc/params.py'):
            text = open(os.path.join(ROOT, path), encoding='utf-8').read()
            assert 'params_env.reader(' in text, path
            assert 'def _f(' not in text, f'{path} завёл свой читатель заново'


class TestTheBotSurvivesTheTypo:

    def test_a_bad_number_no_longer_stops_everything(self, env):
        """
        Итоговая проверка смысла: с опечаткой в настройках стратегии модуль
        параметров обязан загрузиться, а не уронить импорт бота.
        """
        env.setenv('RSIBB_RISK_PCT', '0,5')
        for name in [k for k in list(sys.modules)
                     if k.split('.')[0] in ('rsibb', 'levels', 'smc')]:
            del sys.modules[name]
        import rsibb.params as rp
        assert rp.RISK_PCT == 0.5
