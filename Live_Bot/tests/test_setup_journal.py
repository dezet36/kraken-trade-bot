"""
Журнал сетапов стратегии (setup_journal): строка на сетап из шести источников,
без повторов, с исходом — и выгрузка, которую Excel с русской разметкой
открывает без мастера импорта.
"""

import csv
import io
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

H = 3600


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec='seconds')


T0 = 1_790_000_000          # 2026-09-21, секунды


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Все источники журнала — во временную папку; фантомный режим."""
    import config
    import follow_up
    import llm_journal
    import llm_outcomes
    import paper_broker
    import refused
    import setup_journal
    import shadow
    import strategy_llm

    monkeypatch.setattr(config, 'TRADING_MODE', 'PAPER')
    monkeypatch.setattr(config, 'PAPER_MODE', True)
    paths = {
        (paper_broker, 'JOURNAL_JSON'): 'paper_trades.jsonl',
        (follow_up, 'CSV_PATH'): 'follow_up.csv',
        (setup_journal, 'DROPPED_CSV'): 'orders_dropped.csv',
        (shadow, 'CSV_PATH'): 'shadow_trades.csv',
        (shadow, 'STATE_PATH'): 'shadow_state.json',
        (llm_outcomes, 'CSV_PATH'): 'llm_outcomes.csv',
        (llm_outcomes, 'STATE_PATH'): 'llm_outcomes_state.json',
        (llm_journal, 'CSV_PATH'): 'llm_calls.csv',
        (refused, 'CSV_PATH'): 'refused.csv',
    }
    for (module, name), base in paths.items():
        monkeypatch.setattr(module, name, str(tmp_path / base))
    monkeypatch.setattr(shadow, '_shadows', None)
    monkeypatch.setattr(llm_outcomes, '_watches', None)
    armed = []
    monkeypatch.setattr(strategy_llm, 'current_setups',
                        lambda broker=None, now=None: {'armed': armed, 'pending': [], 'open': []})
    monkeypatch.setattr(setup_journal, '_summary_cache', {'at': 0.0, 'value': None})
    return {'sj': setup_journal, 'armed': armed, 'tmp': tmp_path}


def write_csv(path, columns, rows):
    import csv_journal
    csv_journal.append(path, columns, [{'mode': 'PAPER', **r} for r in rows])


def trade(**kw):
    row = {
        'trade_id': 7, 'strategy': 'FIBO', 'pair': 'BTCUSDT', 'direction': 'LONG',
        'zone': 'Zone_A', 'htf_trend': 'BULLISH',
        'open_time': iso(T0 + 2 * H), 'entry_price': 100.1, 'planned_entry': 100.0,
        'entry_wait_min': 120, 'stop_loss': 90.0, 'tp1': 110.0, 'tp2': 120.0,
        'targets_all': '110;120;130', 'rr': 1.0, 'risk_usd': 100.0,
        'close_time': iso(T0 + 5 * H), 'exit_price': 120.0, 'exit_reason': 'TP2',
        'exit_reason_ru': 'цель 2', 'tps_hit': 2, 'tp_min': '35;170', 'duration_min': 180,
        'cost_share_pct': 1.2, 'fees_usd': 1.5, 'funding_usd': 0.1, 'pnl_usd': 150.0,
        'pnl_r': 1.5, 'result': 'WIN', 'mfe_r': 2.1, 'mae_r': -0.3, 'mfe_min': 170,
        'mae_min': 5, 'first_1r_min': 35, 'r_if_be_1r': 1.5, 'atr_pct': 1.1,
        'hour_utc': 9, 'regime': 'боковик', 'regime_er': 0.21, 'breakeven_set': True,
        'why': 'Лонг · зона A', 'confirmed_ru': 'объём', 'missing_ru': 'дивергенция',
    }
    row.update(kw)
    return row


def write_trades(rows):
    import paper_broker
    with open(paper_broker.JOURNAL_JSON, 'a', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')


def outcome(pair='SUIUSDT', at=T0, gate='', side='LONG', entry=1.0, stop=0.95, tp1=1.1, **kw):
    row = {'at': iso(at), 'pair': pair, 'decision': 'skip' if gate else 'enter', 'gate': gate,
           'side': side, 'price': entry * 1.01, 'entry': entry, 'stop': stop, 'tp1': tp1,
           'best_r': 1.2, 'worst_r': -0.4, 'hit_tp1': 0, 'hit_sl': 0, 'entry_touched': 0,
           'observed_hours': 168}
    row.update(kw)
    return row


def write_outcomes(rows):
    import llm_outcomes
    write_csv(llm_outcomes.CSV_PATH, llm_outcomes.COLUMNS, rows)


def write_refused(rows):
    import refused
    write_csv(refused.CSV_PATH, refused.COLUMNS, rows)


# ── Источники по этапам ──────────────────────────────────────────────────────

class TestTradesComeWithWhatHappenedAfter:
    def test_a_trade_row_carries_the_plan_the_path_and_the_follow_up(self, env):
        import follow_up
        write_trades([trade()])
        write_csv(follow_up.CSV_PATH, follow_up.COLUMNS,
                  [{'trade_id': 7, 'strategy': 'FIBO', 'pair': 'BTCUSDT',
                    'best_after_r': 2.8, 'worst_after_r': 0.4, 'r_4h': 2.2,
                    'hit_tp1_after': 1, 'hit_sl_after': 0}])
        rows = env['sj'].build('FIBO')
        assert len(rows) == 1
        r = rows[0]
        assert (r['stage'], r['outcome']) == ('сделка', 'плюс')
        # Сетап — момент заявки: вход минус ожидание.
        assert r['placed_at'] == iso(T0)
        assert r['targets'] == [110, 120, 130] and r['tp_min'] == [35, 170]
        assert r['regime'] == 'боковик' and r['confirmed'] == 'объём'
        assert r['after_best_r'] == '2.8' and r['after_r_4h'] == '2.2'

    def test_targets_taken_count_the_final_one_too(self, env):
        """Брокер считает только частичные фиксации: выход по TP1 целиком — «0»."""
        write_trades([trade(trade_id=1, exit_reason='TP1', tps_hit=0),
                      trade(trade_id=2, exit_reason='SL', tps_hit=1, result='LOSS')])
        assert [r['tps_hit'] for r in env['sj'].build('FIBO')] == [1, 1]

    def test_another_strategy_is_not_in_the_journal(self, env):
        write_trades([trade(strategy='SMC')])
        assert env['sj'].build('FIBO') == []


class TestOrdersThatDidNotFill:
    def test_a_dropped_order_says_why_and_how_close_price_came(self, env):
        order = {'direction': 'SHORT', 'placed_at': iso(T0), 'planned_entry': 50.0,
                 'limit_price': 49.95, 'stop_loss': 52.0, 'targets': [46.0, 44.0], 'rr': 1.95,
                 'cost_share_pct': 3.1, 'risk_amount': 50.0, 'min_gap_pct': 0.42,
                 'best_run_r': 2.2,
                 'context': {'why': 'Шорт · ордер-блок', 'confirmed': ['слом'], 'missing': [],
                             'factors': ['bos', 'ob'], 'regime': 'падение', 'regime_er': 0.4}}
        env['sj'].record_dropped('SMC', 'ETHUSDT', order, 'цена дошла до цели без нас',
                                 (T0 + 6 * H) * 1000)
        env['sj'].record_dropped('SMC', 'ETHUSDT', {}, 'пустая заявка не пишется', 0)
        rows = env['sj'].build('SMC')
        assert len(rows) == 1
        r = rows[0]
        assert (r['stage'], r['outcome']) == ('заявка снята', 'цель без входа')
        assert r['gate'] == 'цена дошла до цели без нас'
        assert r['min_gap_pct'] == '0.42' and r['best_run_r'] == '2.2'
        assert r['targets'] == [46, 44] and r['factors'] == 'bos, ob'
        assert r['closed_at'] == iso(T0 + 6 * H)

    def test_expiry_and_operator_cancel_are_told_apart(self, env):
        base = {'direction': 'LONG', 'placed_at': iso(T0), 'planned_entry': 1.0,
                'limit_price': 1.0, 'stop_loss': 0.9, 'targets': [1.2]}
        env['sj'].record_dropped('FIBO', 'A', base, 'лимит не заполнен за 72ч', T0 * 1000)
        env['sj'].record_dropped('FIBO', 'B', base, 'снят оператором', T0 * 1000)
        env['sj'].record_dropped('FIBO', 'C', base, 'сетап разрушен ($0.88)', T0 * 1000)
        got = {r['pair']: r['outcome'] for r in env['sj'].build('FIBO')}
        assert got == {'A': 'не налилась за срок', 'B': 'снята оператором', 'C': 'сетап разрушен'}


class TestShadows:
    def test_finished_and_running_shadows_but_not_the_one_opened_later(self, env):
        import shadow
        write_csv(shadow.CSV_PATH, shadow.COLUMNS, [
            {'strategy': 'SMC', 'pair': 'LTCUSDT', 'direction': 'LONG', 'gate': 'направленный кэп',
             'first_at': iso(T0), 'refusals': 219, 'entry': 80.0, 'stop': 78.0,
             'targets': '[84.0, 86.0]', 'outcome': 'цель', 'targets_hit': 2, 'result_r': 2.4,
             'best_r': 3.0, 'worst_r': -0.2, 'fill_hours': 1.5, 'closed_hours': 9.0},
            {'strategy': 'SMC', 'pair': 'DOTUSDT', 'direction': 'LONG', 'gate': 'кулдаун',
             'first_at': iso(T0), 'refusals': 1, 'entry': 4.0, 'stop': 3.9,
             'targets': '[4.2]', 'outcome': 'открыт позже'},
        ])
        shadow.watch('SMC', {'trading_pair': 'LINKUSDT', 'setup': {'type': 'SHORT'},
                             'params': {'entry': 20.0, 'stop_loss': 21.0, 'tp_targets': [18.0],
                                        'tp_fractions': [1.0]}},
                     'предел портфеля', now_ms=(T0 + H) * 1000)
        rows = env['sj'].build('SMC')
        assert [(r['pair'], r['stage'], r['outcome']) for r in rows] == [
            ('LTCUSDT', 'не пустил предел', 'цель'),
            ('LINKUSDT', 'не пустил предел', 'идёт')]
        assert rows[0]['targets'] == [84.0, 86.0] and rows[0]['refusals'] == '219'


class TestTheLiveBook:
    def test_pending_orders_and_open_positions(self, env):
        ms = (T0 + H) * 1000

        class Broker:
            @staticmethod
            def pending(strategy):
                return {'XRPUSDT': {'direction': 'LONG', 'placed_at': iso(T0), 'planned_entry': 2.0,
                                    'limit_price': 2.002, 'stop_loss': 1.9, 'targets': [2.3],
                                    'rr': 3.0, 'risk_amount': 40.0, 'min_gap_pct': 0.8,
                                    'context': {'why': 'Лонг'}}}

            @staticmethod
            def positions(strategy):
                return {'ADAUSDT': {'direction': 'SHORT', 'trade_id': 12, 'placed_ts': ms,
                                    'opened_ts': ms + 600_000, 'opened_at': iso(T0 + H + 600),
                                    'last_ts': ms + 3_600_000, 'entry_price': 1.0,
                                    'planned_entry': 1.0, 'initial_stop': 1.05, 'targets': [0.9],
                                    'rr': 2.0, 'risk_amount': 50.0, 'tp_hit': 0,
                                    'mfe_price': 0.96, 'mae_price': 1.01, 'mfe_ts': ms + 1_800_000,
                                    'mae_ts': ms + 600_000, 'context': {'why': 'Шорт'}}}

        rows = env['sj'].build('RSIBB', broker=Broker())
        by = {r['stage']: r for r in rows}
        assert by['заявка ждёт']['pair'] == 'XRPUSDT' and by['заявка ждёт']['min_gap_pct'] == 0.8
        pos = by['позиция открыта']
        assert pos['entry_wait_min'] == 10 and pos['duration_min'] == 50
        assert pos['mfe_r'] == pytest.approx(0.8) and pos['mfe_min'] == 20


# ── Планы ИИ ─────────────────────────────────────────────────────────────────

class TestLlmPlans:
    def test_a_plan_refused_by_code_is_joined_with_its_analysis(self, env):
        import llm_journal
        write_outcomes([outcome(gate='план против структуры', entry=1.0, stop=0.95, tp1=1.1,
                                entry_touched=1, entry_hours=2.0, hit_sl=1, sl_hours=5.0)])
        write_csv(llm_journal.CSV_PATH, llm_journal.COLUMNS, [
            {'at': iso(T0 - 300), 'pair': 'SUIUSDT', 'decision': 'skip',
             'gate': 'план против структуры', 'detail': 'вход за сломом',
             'why': 'отскок от имбаланса', 'analysis': 'тренд 4ч вниз', 'trigger': 'retest',
             'stop_why': 'за минимумом', 'tp_why': 'пул', 'p': 0.6, 'votes': 3},
            # Другая пара и другой отказ — не его разбор.
            {'at': iso(T0 - 200), 'pair': 'SUIUSDT', 'decision': 'skip',
             'gate': 'модель пропустила', 'why': 'не то'},
        ])
        rows = env['sj'].build('LLM')
        assert len(rows) == 1
        r = rows[0]
        assert (r['stage'], r['gate'], r['outcome']) == ('план отклонён', 'план против структуры', 'стоп')
        assert r['why'] == 'отскок от имбаланса' and r['llm_stop_why'] == 'за минимумом'
        assert r['detail'] == 'вход за сломом' and r['rr'] == 2.0

    def test_a_plan_that_became_a_trade_is_not_listed_twice(self, env):
        write_outcomes([outcome(pair='ARBUSDT', at=T0, entry=0.5, stop=0.48, tp1=0.56)])
        write_trades([trade(strategy='LLM', pair='ARBUSDT', planned_entry=0.5,
                            open_time=iso(T0 + 3 * H), entry_wait_min=170)])
        rows = env['sj'].build('LLM')
        assert [r['stage'] for r in rows] == ['сделка']

    def test_a_plan_that_died_armed_says_how(self, env):
        write_outcomes([outcome(pair='NEARUSDT', at=T0, entry=3.0, stop=2.9, tp1=3.3,
                                hit_tp1=1, tp_hours=4.0)])
        write_refused([{'at': iso(T0 + 12 * H), 'strategy': 'LLM', 'pair': 'NEARUSDT',
                        'gate': 'ИИ: условие не наступило', 'detail': 'close_above 3.05 за 12 ч'}])
        r = env['sj'].build('LLM')[0]
        assert (r['stage'], r['gate'], r['outcome']) == \
            ('план не дошёл до заявки', 'условие не наступило', 'цель без входа')
        assert r['detail'] == 'close_above 3.05 за 12 ч'

    def test_a_newer_plan_takes_the_fate_written_after_it(self, env):
        """Новый план сменяет взведённый: судьба после него — уже его."""
        write_outcomes([outcome(pair='OPUSDT', at=T0, entry=1.0),
                        outcome(pair='OPUSDT', at=T0 + 2 * H, entry=1.02)])
        write_refused([{'at': iso(T0 + 14 * H), 'strategy': 'LLM', 'pair': 'OPUSDT',
                        'gate': 'ИИ: условие не наступило'}])
        rows = env['sj'].build('LLM')
        assert [(r['entry'], r['stage'], r['gate']) for r in rows] == [
            ('1.0', 'план не дошёл до заявки', 'сменён новым планом'),
            ('1.02', 'план не дошёл до заявки', 'условие не наступило')]

    def test_the_order_belongs_to_the_last_plan_before_it(self, env):
        """
        20.09.2026 DOTUSDT: план с тем же входом взводился дважды, заявка
        выставлена через час после второго. Она — второго; первый сменён.
        """
        write_outcomes([outcome(pair='DOTUSDT', at=T0, side='SHORT', entry=1.1111, stop=1.15, tp1=1.05),
                        outcome(pair='DOTUSDT', at=T0 + 2 * H, side='SHORT', entry=1.1111,
                                stop=1.15, tp1=1.05)])
        write_trades([trade(strategy='LLM', pair='DOTUSDT', direction='SHORT', planned_entry=1.1111,
                            open_time=iso(T0 + 5 * H), entry_wait_min=120)])
        rows = env['sj'].build('LLM')
        assert [(r['placed_at'], r['stage'], r['gate'] if r['stage'] != 'сделка' else '') for r in rows] == [
            (iso(T0), 'план не дошёл до заявки', 'сменён новым планом'),
            (iso(T0 + 3 * H), 'сделка', '')]

    def test_a_now_plan_without_a_trace_is_not_called_replaced(self, env):
        """План «входить сейчас» не взводится — сменять нечего; честно: не записано."""
        write_outcomes([outcome(pair='INJUSDT', at=T0, trigger_when='now'),
                        outcome(pair='INJUSDT', at=T0 + H, gate='мало конфлюенса')])
        rows = env['sj'].build('LLM')
        assert (rows[0]['stage'], rows[0]['gate']) == ('план принят, заявки нет', 'судьба не записана')

    def test_an_armed_plan_waits(self, env):
        write_outcomes([outcome(pair='APTUSDT', at=T0, entry=5.0, stop=4.8, tp1=5.6)])
        env['armed'].append({'pair': 'APTUSDT', 'entry': 5.0})
        assert env['sj'].build('LLM')[0]['stage'] == 'план ждёт условия'

    def test_a_broker_refusal_of_the_plan_is_named(self, env):
        write_outcomes([outcome(pair='TIAUSDT', at=T0, entry=6.0, stop=5.8, tp1=6.6)])
        write_refused([{'at': iso(T0 + 30), 'strategy': 'LLM', 'pair': 'TIAUSDT',
                        'direction': 'LONG', 'entry': 6.0, 'gate': 'пара занята',
                        'detail': 'держит SMC'}])
        r = env['sj'].build('LLM')[0]
        assert (r['stage'], r['gate'], r['detail']) == ('план принят, заявки нет', 'пара занята', 'держит SMC')

    def test_a_running_observation_is_in_the_journal_too(self, env):
        import llm_outcomes
        llm_outcomes.watch('WLDUSDT', {'ok': False, 'gate': 'мало конфлюенса',
                                       'plan': {'side': 'SHORT', 'entry': 2.0, 'stop': 2.1,
                                                'targets': [1.8]}},
                           price=1.99, ts=T0 * 1000, at=iso(T0))
        r = env['sj'].build('LLM')[0]
        assert (r['pair'], r['stage'], r['gate']) == ('WLDUSDT', 'план отклонён', 'мало конфлюенса')

    def test_plans_are_only_in_the_llm_journal(self, env):
        write_outcomes([outcome()])
        assert env['sj'].build('SMC') == []


class TestPlanOutcome:
    @pytest.mark.parametrize('obs, expected', [
        ({'entry_touched': 0, 'hit_tp1': 1}, 'цель без входа'),
        ({'entry_touched': 0, 'hit_tp1': 0}, 'вход не задет'),
        ({'entry_touched': 1, 'entry_hours': 3, 'hit_tp1': 1, 'tp_hours': 1}, 'цель до входа'),
        ({'entry_touched': 1, 'entry_hours': 1, 'hit_tp1': 1, 'tp_hours': 5,
          'hit_sl': 1, 'sl_hours': 3}, 'стоп'),
        ({'entry_touched': 1, 'entry_hours': 1, 'hit_tp1': 1, 'tp_hours': 3,
          'hit_sl': 1, 'sl_hours': 3}, 'стоп'),
        ({'entry_touched': 1, 'entry_hours': 1, 'hit_tp1': 1, 'tp_hours': 3,
          'hit_sl': 1, 'sl_hours': 6}, 'цель'),
        ({'entry_touched': 1, 'entry_hours': 1}, 'ни цели, ни стопа'),
    ])
    def test_the_first_level_after_the_entry_decides(self, obs, expected):
        import setup_journal
        assert setup_journal._plan_outcome({k: str(v) for k, v in obs.items()}) == expected


# ── Сводка и выгрузка ────────────────────────────────────────────────────────

class TestSummary:
    def test_counts_by_stage_and_the_trades_total(self, env):
        write_trades([trade(), trade(trade_id=8, pnl_r=-1.0, pnl_usd=-100.0, result='LOSS')])
        env['sj'].record_dropped('FIBO', 'ETHUSDT', {'direction': 'LONG', 'placed_at': iso(T0)},
                                 'лимит не заполнен за 72ч', T0 * 1000)
        got = env['sj'].summary(('FIBO', 'LLM'), now=1000.0)
        assert got['FIBO'] == {'stages': {'сделка': 2, 'заявка снята': 1}, 'total': 3,
                               'trades_r': 0.5}
        assert got['LLM']['total'] == 0

    def test_it_is_not_rebuilt_on_every_poll(self, env):
        first = env['sj'].summary(('FIBO',), now=1000.0)
        write_trades([trade()])
        assert env['sj'].summary(('FIBO',), now=1060.0) is first
        assert env['sj'].summary(('FIBO',), now=1000.0 + env['sj'].SUMMARY_TTL_S + 1)['FIBO']['total'] == 1


class TestCsvForExcel:
    def read(self, body):
        assert body.startswith('﻿'.encode('utf-8'))
        return list(csv.reader(io.StringIO(body.decode('utf-8-sig')), delimiter=';'))

    def test_russian_headers_semicolons_and_decimal_commas(self, env):
        write_trades([trade()])
        table = self.read(env['sj'].to_csv(env['sj'].build('FIBO')))
        header, row = table[0], dict(zip(table[0], table[1]))
        assert header[:3] == ['этап', 'исход', 'стратегия']
        assert len(header) == len(env['sj'].COLUMNS)
        assert row['итог, R'] == '1,5' and row['вход'] == '100,1'
        assert row['цели'] == '110 / 120 / 130' and row['цели взяты на минуте'] == '35 / 170'
        assert row['вход, UTC'] == datetime.fromtimestamp(T0 + 2 * H, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        assert row['стоп в безубытке'] == 'да'
        # Текст с точкой — не число, запятой не получает.
        assert row['почему'] == 'Лонг · зона A'

    def test_an_empty_journal_is_just_the_header(self, env):
        assert len(self.read(env['sj'].to_csv([]))) == 1


class TestRegimeForTheJournal:
    def frame(self, n=260):
        import pandas as pd
        closes = [100 + (i % 7) for i in range(n)]
        return pd.DataFrame({'timestamp': pd.date_range('2026-01-01', periods=n, freq='D'),
                             'close': closes})

    def test_counted_once_a_day(self, monkeypatch):
        import market_regime
        monkeypatch.setattr(market_regime, '_btc_cache', {})
        calls = []

        def fetch(tf, limit, symbol):
            calls.append((tf, limit, symbol))
            return self.frame()

        day = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        name, er = market_regime.btc_regime(fetch, now=day)
        assert name in ('рост', 'падение', 'боковик', 'неизвестен') and er is not None
        assert market_regime.btc_regime(fetch, now=day.replace(hour=23)) == (name, er)
        assert len(calls) == 1 and calls[0][0] == '1d' and calls[0][2] == market_regime.SYMBOL
        assert market_regime.last_btc_regime(now=day) == (name, er)
        assert market_regime.last_btc_regime(now=day.replace(day=26)) == ('', None)

    def test_a_failed_fetch_is_a_dash_and_is_retried(self, monkeypatch):
        import market_regime
        monkeypatch.setattr(market_regime, '_btc_cache', {})
        day = datetime(2026, 9, 25, tzinfo=timezone.utc)
        assert market_regime.btc_regime(lambda *a: None, now=day) == ('', None)

        def boom(*a):
            raise RuntimeError('биржа молчит')
        assert market_regime.btc_regime(boom, now=day) == ('', None)
        assert market_regime.btc_regime(lambda *a: self.frame(), now=day)[1] is not None


class TestDashboardExport:
    def test_a_journal_per_strategy_and_a_clear_refusal_for_an_unknown_one(self, env):
        import dashboard
        write_trades([trade(strategy='SMC')])
        body, name = dashboard._journal_export('SMC')
        assert name.startswith('journal-SMC-') and name.endswith('.csv')
        assert 'сделка' in body.decode('utf-8-sig')
        with pytest.raises(ValueError):
            dashboard._journal_export('NOPE')

    def test_the_page_has_a_button_per_strategy(self):
        page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 'dashboard.html'), encoding='utf-8').read()
        assert 'id="setup-journal"' in page and 'data-export="journal-${esc(k)}"' in page
        assert "/api/journal.csv?strategy=" in page
