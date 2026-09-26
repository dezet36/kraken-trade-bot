"""
Правило проекта (CLAUDE.md): правка одной стратегии не меняет другие.

Данные с биржи и структурные элементы — общий слой; каждая стратегия
читает оттуда и решает у себя. Этот набор проверяет правило ДЕЛОМ, а не
словами: считает отпечаток того, что видит каждый потребитель на одних и
тех же свечах, крутит параметры РЕШЕНИЙ одной стратегии — и требует, чтобы
отпечатки остальных не сдвинулись ни на бит.

Что уже ломалось до этого теста (21.09.2026): срок заявки ИИ брался из
параметра Фибоначчи; предел издержек из config запирал SMC и уровни;
ИИ импортировал стратегию SMC ради структуры; настройка оператора писалась
в общие параметры ядра.

Граф импортов по правилам пакетов (пакет не читает config, биржу, чужой
пакет) держит tests/test_strategy_isolation.py; здесь — поведение.
"""

import json
import os
import re
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Один рынок для всех ──────────────────────────────────────────────────────

def market(bars=2000, seed=11):
    # 2000 часов: направлению старшего порядка нужно 30 дневных свечей, на
    # 700 часах SMC не доходит до своих решений — bias «не определён» всегда.
    rng = np.random.default_rng(seed)
    drift = np.concatenate([
        np.full(180, 0.0013), np.full(140, -0.0011),
        np.full(160, 0.0004), np.full(bars, 0.0009),
    ])[:bars]
    closes = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.006, bars)))
    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    span = np.abs(closes - opens) + closes * rng.uniform(0.001, 0.005, bars)
    return pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=bars, freq='h', tz='UTC'),
        'open': opens,
        'high': np.maximum(opens, closes) + span * rng.uniform(0, 0.6, bars),
        'low': np.minimum(opens, closes) - span * rng.uniform(0, 0.6, bars),
        'close': closes,
        'volume': rng.uniform(50, 500, bars),
    })


def frames_of(df_1h):
    def agg(rule):
        return (df_1h.set_index('timestamp').resample(rule)
                .agg({'open': 'first', 'high': 'max', 'low': 'min',
                      'close': 'last', 'volume': 'sum'})
                .dropna().reset_index())
    return {'bias': agg('1D'), 'htf': agg('4h'), 'poi': df_1h}


def _fp(obj):
    """Отпечаток чего угодно: json с приведением незнакомого к строке."""
    return json.dumps(obj, sort_keys=True, default=str)


# ── Что видит каждый потребитель ─────────────────────────────────────────────

def smc_decisions(df):
    from smc import signal as smc_signal
    ctx = smc_signal.build_context(frames_of(df.copy()), pair='TEST')
    out = []
    for i in range(800, len(df), 7):
        setup, reason = ctx.evaluate(i)
        if setup is None:
            out.append(reason)
        else:
            t = setup['params']
            out.append((setup['direction'], setup['poi']['type'], setup['poi']['index'],
                        round(setup['confluence'], 6), round(t['entry'], 8),
                        round(t['stop_loss'], 8), [round(x, 8) for x in t['targets']]))
    return _fp(out)


def llm_structure(df):
    """Разметка структуры для модели — по тому же общему контексту."""
    import llm_market
    from smc import signal as smc_signal
    ctx = smc_signal.build_context(frames_of(df.copy()), pair='TEST')
    price = float(df['close'].iloc[-1])
    return _fp(llm_market._smc_from_context(ctx, price))


def llm_levels(df):
    import llm_context
    found, _ = llm_context.levels(df)
    return _fp([(l.get('id'), round(float(l.get('price', 0)), 8), l.get('kind'), l.get('strength'))
                if isinstance(l, dict) else str(l) for l in found])


def levels_decisions(df):
    from levels import core
    high, low, close = (df[c].to_numpy(dtype=float) for c in ('high', 'low', 'close'))
    volume = df['volume'].to_numpy(dtype=float)
    lv = core.build_levels(high, low)
    a = core.atr(high, low, close)
    out = [[round(float(x), 8) for x in (l if isinstance(l, (list, tuple)) else [l])] if not isinstance(l, dict)
           else {k: (round(float(v), 8) if isinstance(v, (int, float, np.floating)) else str(v)) for k, v in l.items()}
           for l in lv]
    for i in range(150, len(df), 17):
        setup, reason = core.evaluate(high, low, close, volume, i, levels=lv, atr_values=a)
        out.append(reason if setup is None else {k: (round(float(v), 8) if isinstance(v, (int, float, np.floating)) else str(v))
                                                 for k, v in setup.items()})
    return _fp(out)


def rsibb_decisions(df):
    from rsibb import core
    ind = core.indicators(*(df[c].to_numpy(dtype=float) for c in ('open', 'high', 'low', 'close')))
    out = []
    for i in range(150, len(df), 17):
        setup, reason = core.evaluate(ind, i)
        if setup is None:
            out.append(reason)
            continue
        trade = core.build_trade(setup)
        out.append((str(setup.get('direction')), round(float(setup.get('band', 0)), 8),
                    None if trade is None else {k: round(float(v), 8) for k, v in trade.items()
                                                if isinstance(v, (int, float, np.floating))}))
    return _fp(out)


def profiles():
    import strategy_profile
    return _fp({n: strategy_profile.describe(n) for n in ('SMC', 'LEVELS', 'RSIBB', 'LLM')})


READERS = {
    'SMC': smc_decisions,
    'LLM-структура': llm_structure,
    'LLM-уровни': llm_levels,
    'LEVELS': levels_decisions,
    'RSIBB': rsibb_decisions,
}


@pytest.fixture(scope='module')
def df():
    return market()


@pytest.fixture(scope='module')
def baseline(df):
    return {name: fn(df) for name, fn in READERS.items()} | {'профили': profiles()}


# ── Правка решений одной стратегии не трогает остальных ─────────────────────

class TestDecisionParamsAreIsolated:

    def test_smc_decisions_do_not_reach_anyone_else(self, df, baseline, monkeypatch):
        from smc import params
        for name, value in {
            'MIN_RR': 1.0, 'MAX_RR': 3.0, 'TP_MODE': 'liquidity', 'MIN_SL_PCT': 0.02,
            'SL_MODE': 'aggressive', 'POI_TYPES_ENABLED': (), 'REQUIRE_KILLZONE': False,
            'KILLZONE_AS_GATE': True, 'MIN_CONFLUENCE_SCORE': 0.5, 'RISK_PER_TRADE_PCT': 3.0,
            'POI_ENTRY_DEPTH': 0.5, 'REQUIRE_PREMIUM_DISCOUNT': False,
            'PENDING_ORDER_MAX_HOURS': 1.0, 'COOLDOWN_HOURS': 99.0, 'MAX_ENTRY_COST_SHARE_PCT': 50.0,
            'FILL_THROUGH_MARKET': True,
        }.items():
            assert name in params.DECISION, f'{name} не объявлен решением SMC'
            monkeypatch.setattr(params, name, value)
        assert smc_decisions(df) != baseline['SMC'], 'правка решений SMC должна менять сделки SMC — иначе тест пуст'
        for name, fn in READERS.items():
            if name != 'SMC':
                assert fn(df) == baseline[name], f'{name} изменился от правки решений SMC'
        # Свои величины исполнения у SMC сдвинулись, чужие — нет.
        import strategy_profile
        assert strategy_profile.cooldown_hours('SMC') == 99.0
        assert strategy_profile.fills_through_market('SMC') is True
        for other in ('LEVELS', 'RSIBB', 'LLM'):
            assert _fp(strategy_profile.describe(other)) == _fp(json.loads(baseline['профили'])[other])

    def test_levels_decisions_do_not_reach_anyone_else(self, df, baseline, monkeypatch):
        from levels import params
        for name, value in {
            'TRIGGER_ATR': 5.0, 'MIN_GAP_ATR': 0.0, 'PIERCE_ATR': 1.0, 'RECLAIM_BARS': 20,
            'VOLUME_RATIO': 0.1, 'MIN_STOP_PCT': 5.0, 'MIN_TARGET_R': 0.1, 'RISK_PCT': 5.0,
            'EXPIRY_HOURS': 1.0, 'COOLDOWN_HOURS': 99.0, 'MAX_ENTRY_COST_SHARE_PCT': 50.0,
            'MAX_HOLD_HOURS': 1.0, 'FILL_THROUGH_MARKET': True,
        }.items():
            monkeypatch.setattr(params, name, value)
        assert levels_decisions(df) != baseline['LEVELS'], 'правка уровней должна менять уровни — иначе тест пуст'
        for name, fn in READERS.items():
            if name != 'LEVELS':
                assert fn(df) == baseline[name], f'{name} изменился от правки решений уровней'

    def test_rsibb_decisions_do_not_reach_anyone_else(self, df, baseline, monkeypatch):
        from rsibb import params
        for name, value in {
            'RSI_LOW': 90.0, 'RSI_HIGH': 10.0, 'RSI_MODE': 'level', 'ADX_MAX': 5.0,
            'ENTRY_MODE': 'reclaim', 'TARGET_FRAC': 0.2, 'EXPIRY_BARS': 1,
            'COOLDOWN_HOURS': 99.0, 'MAX_ENTRY_COST_SHARE_PCT': 50.0,
            'FILL_THROUGH_MARKET': True,
        }.items():
            monkeypatch.setattr(params, name, value)
        assert rsibb_decisions(df) != baseline['RSIBB'], 'правка Боллинджера должна менять Боллинджер — иначе тест пуст'
        for name, fn in READERS.items():
            if name != 'RSIBB':
                assert fn(df) == baseline[name], f'{name} изменился от правки решений Боллинджера'

    def test_llm_settings_do_not_reach_anyone_else(self, df, baseline, monkeypatch):
        import strategy_profile
        cfg = strategy_profile._config()
        for name, value in {
            'LLM_COOLDOWN_HOURS': 99.0, 'LLM_MAX_ENTRY_COST_SHARE_PCT': 50.0,
            'LLM_TRIGGER_TTL_H': 1, 'LLM_LIMIT_ENTRY_OFFSET_PCT': 0.01,
            'LLM_FILL_THROUGH_MARKET': False,
        }.items():
            monkeypatch.setattr(cfg, name, value, raising=False)
        for name, fn in READERS.items():
            assert fn(df) == baseline[name], f'{name} изменился от настроек ИИ'
        before = json.loads(baseline['профили'])
        for other in ('SMC', 'LEVELS', 'RSIBB'):
            assert _fp(strategy_profile.describe(other)) == _fp(before[other])

    def test_common_config_values_reach_only_fibo(self, df, baseline, monkeypatch):
        import strategy_profile
        cfg = strategy_profile._config()
        for name, value in {
            'MAX_ENTRY_COST_SHARE_PCT': 0.01, 'COOLDOWN_HOURS': 0.01,
            'PENDING_ORDER_MAX_HOURS': 0.01, 'MAX_POSITION_HOLD_HOURS': 0.01,
            'FIBO_FILL_THROUGH_MARKET': True,
        }.items():
            monkeypatch.setattr(cfg, name, value)
        assert profiles() == baseline['профили']
        assert strategy_profile.cost_limit_pct('FIBO') == pytest.approx(0.01)
        assert strategy_profile.fills_through_market('FIBO') is True


# ── Общий слой: настройки оператора не пишутся в структуру ───────────────────

class TestTheSharedLayerIsNotWrittenByStrategies:

    def test_operator_settings_land_only_in_smc_decisions(self, monkeypatch):
        import strategy_smc
        # Модули берём те, что держит САМ адаптер: другие наборы перезагружают
        # settings_store и smc.params, и свежий import был бы чужим объектом.
        params = strategy_smc.smc_params
        monkeypatch.setattr(strategy_smc.settings, 'risk_pct', lambda s: 7.5)
        monkeypatch.setattr(strategy_smc.settings, 'min_stop_pct', lambda s: 4.2)
        before = _fp(params.structural_snapshot())
        strategy_smc._apply_settings()
        assert _fp(params.structural_snapshot()) == before, 'настройка оператора записана в структуру рынка'
        assert params.RISK_PER_TRADE_PCT == 7.5 and params.MIN_SL_PCT == 4.2
        assert set(strategy_smc._OPERATOR_SETTINGS) <= params.DECISION
        monkeypatch.setattr(params, 'RISK_PER_TRADE_PCT', 1.0)
        monkeypatch.setattr(params, 'MIN_SL_PCT', 0.005)

    def test_a_setting_aimed_at_the_structure_is_refused(self, monkeypatch):
        import strategy_smc
        monkeypatch.setitem(strategy_smc._OPERATOR_SETTINGS, 'SWING_N_STRUCT', lambda: 5)
        before = strategy_smc.smc_params.SWING_N_STRUCT
        with pytest.raises(RuntimeError):
            strategy_smc._apply_settings()
        assert strategy_smc.smc_params.SWING_N_STRUCT == before

    def test_every_smc_param_is_declared_on_one_side(self):
        from smc import params
        names = {n for n in dir(params) if n.isupper() and not n.startswith('_')} - {'STRUCTURAL', 'DECISION'}
        assert not names - params.STRUCTURAL - params.DECISION, 'параметр без стороны'
        assert not params.STRUCTURAL & params.DECISION
        assert not (params.STRUCTURAL | params.DECISION) - names

    def test_the_structure_really_is_shared(self, df, baseline, monkeypatch):
        """
        Контроль, что отпечатки не пусты: правка ОПРЕДЕЛЕНИЯ (часть I)
        меняет и решения SMC, и разметку для модели — это одно и то же место.
        """
        from smc import params
        monkeypatch.setattr(params, 'SWING_N_STRUCT', 5)
        monkeypatch.setattr(params, 'FVG_MIN_SIZE_PCT', 0.5)
        assert llm_structure(df) != baseline['LLM-структура']
        assert smc_decisions(df) != baseline['SMC']


# ── Граф импортов: стратегии не импортируют друг друга ───────────────────────

STRATEGY_MODULES = {
    'SMC': ('strategy_smc.py',),
    'LEVELS': ('strategy_levels.py',),
    'RSIBB': ('strategy_rsibb.py',),
    'LLM': ('strategy_llm.py', 'llm_market.py', 'llm_context.py', 'llm_decide.py',
            'llm_record.py', 'llm_urgency.py', 'llm_prompt.py', 'llm_grammar.py'),
}
SHARED = ('market_structure.py', 'smc/signal.py', 'smc/structure.py', 'smc/swings.py',
          'smc/liquidity.py', 'smc/imbalance.py', 'smc/poi.py', 'smc/fib.py',
          'smc/sessions.py', 'levels/core.py', 'rsibb/core.py', 'liquidity/core.py',
          'exchange.py', 'market_regime.py', 'strategy_profile.py')
ADAPTERS = re.compile(r'^\s*(import|from)\s+(strategy_smc|strategy_levels|strategy_rsibb|strategy_llm)\b', re.M)


def _src(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as fh:
        return fh.read()


class TestNoStrategyImportsAnother:

    @pytest.mark.parametrize('owner', sorted(STRATEGY_MODULES))
    def test_strategy_modules(self, owner):
        own = {f'strategy_{owner.lower()}'}
        for path in STRATEGY_MODULES[owner]:
            if not os.path.exists(os.path.join(ROOT, path)):
                continue
            hits = {m.group(2) for m in ADAPTERS.finditer(_src(path))} - own
            assert not hits, f'{path} импортирует чужую стратегию: {sorted(hits)}'

    @pytest.mark.parametrize('path', SHARED)
    def test_shared_layer_knows_no_strategy(self, path):
        src = _src(path)
        hits = {m.group(2) for m in ADAPTERS.finditer(src)}
        assert not hits, f'общий слой {path} импортирует стратегию: {sorted(hits)}'
        if path.startswith(('smc/', 'levels/', 'rsibb/', 'liquidity/')):
            assert not re.search(r'^\s*(import|from)\s+(config|settings_store)\b', src, re.M), (
                f'{path}: ядро читает config/настройки — оно должно считать по числам своего params')
