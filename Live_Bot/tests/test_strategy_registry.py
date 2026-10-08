"""
Реестр стратегий (strategies/registry.py): новая стратегия встраивается своим
пакетом и записью в реестре — без правки общих модулей (реорганизация, этап 1).

Проверяется, что
  - у каждой записи есть адаптер с единым набором функций;
  - общие модули (брокер, настройки, Telegram) берут список стратегий отсюда;
  - стратегия узнаётся по своему сигналу;
  - панель получает имя и цвет каждой стратегии из реестра.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies import registry  # noqa: E402

FUNCTIONS = ('scan', 'build_signal', 'profile', 'geometry')


@pytest.mark.parametrize('code', registry.codes())
def test_adapter_has_the_uniform_functions(code):
    mod = registry.adapter(code)
    for name in FUNCTIONS:
        assert callable(getattr(mod, name, None)), f'{code}: в адаптере нет {name}()'


@pytest.mark.parametrize('code', registry.codes())
def test_profile_is_a_dict_of_known_keys(code):
    known = {'expiry_hours', 'cooldown_hours', 'cost_limit_pct', 'max_hold_hours',
             'drops_at_target', 'fills_through_market', 'min_stop_pct', 'limit_offset_pct',
             # с каким пределом в одну сторону стратегия измерена — счёт берёт его
             # по умолчанию (этап 2)
             'max_same_direction'}
    prof = registry.adapter(code).profile()
    assert isinstance(prof, dict)
    assert set(prof) <= known, f'{code}: незнакомые ключи профиля {set(prof) - known}'


def test_common_modules_take_the_list_from_the_registry():
    import paper_broker
    import settings_store
    import tg_format
    assert tuple(paper_broker.STRATEGIES) == registry.codes()
    assert tuple(settings_store.STRATEGIES) == registry.codes()
    assert tuple(tg_format.ORDER) == registry.codes()
    assert tg_format.NAMES == registry.shorts()


def test_codes_are_unique_and_live_cycle_is_a_subset():
    codes = registry.codes()
    assert len(codes) == len(set(codes))
    assert set(registry.live_codes()) <= set(codes)
    assert 'LLM' not in registry.live_codes(), 'ИИ торгуется только на бумаге'


@pytest.mark.parametrize('code', registry.codes())
def test_each_strategy_is_recognised_by_its_signal(code):
    entry = registry.get(code)
    signal = {entry.signal_keys[0]: {'x': 1}}
    assert registry.strategy_of(signal) == code


def test_ai_is_recognised_before_smc():
    """Сигнал ИИ в режиме «правила» несёт и раздел SMC — это всё равно ИИ."""
    assert registry.strategy_of({'llm': {'p': 1}, 'smc': {'poi_type': 'OB'}}) == 'LLM'


def test_unknown_signal_is_nobodys():
    assert registry.strategy_of({}) == ''


def test_dashboard_gets_every_name_and_colour():
    import dashboard
    page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'dashboard.html')
    body = dashboard._with_strategy_registry(open(page, 'rb').read()).decode('utf-8')
    assert 'window.STRATEGY_REGISTRY' in body
    for entry in registry.REGISTRY:
        assert f'"code": "{entry.code}"' in body
        assert f'--{entry.code.lower()}: {entry.color_light};' in body
        assert f'--{entry.code.lower()}: {entry.color_dark};' in body


def test_unknown_strategy_is_refused_by_the_cycle():
    """Стратегия без записи в реестре не торгует: ни сканера, ни сборки сигнала."""
    import bot
    signal, df = bot._build_signal({'pair': 'ETHUSDT', 'signal': {'setup': {'type': 'LONG'}}},
                                   'NOBODY', 10_000)
    assert signal is None and df is None
