"""
Анализ дня по активу (strategies/llm/llm_daily, решение владельца 15.10.2026).

Что держится:
- настройки DAILY: активы только из пула, без повторов, не больше MAX_PAIRS;
  час — целый 0–23; правка ложится в историю настроек;
- расписание: по одному активу в час, последний — к часу отправки; сбой модели
  повторяется один раз, занятая модель переносит разбор на следующий час;
- числа считает код: структура по таймфреймам, R:R сетапов модели; правила
  торговой ИИ-стратегии и её счёт в вопрос не попадают;
- вопрос помещается в окно модели (свечи часового графика уходят первыми);
- выжимка в Telegram — экранированный HTML не длиннее 4000 знаков, со своим
  тумблером; полный разбор — на панели;
- промт меняется только с экспортом docs/Промт_ИИ_анализ_дня.txt;
- модуль не читает слой счетов и чужие стратегии; сбой не бросается в цикл бота.
"""

import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from infra import outbox  # noqa: E402
from strategies.llm import llm_daily  # noqa: E402

CFG = {'enabled': True, 'pairs': ['BTCUSDT', 'ETHUSDT'], 'hour_utc': 6}


def at(hour, minute=10, day=15):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def daily(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_daily.config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(llm_daily.config, 'LLM_SERVER_URL', 'http://127.0.0.1:1', raising=False)
    monkeypatch.setattr(llm_daily, '_job', {'thread': None, 'pair': None})
    return tmp_path


ANSWER = """Актив и данные — 1D, 4H, 15m, OI, funding есть; новостей нет.
1) MARKET REGIME — HTF markup, LTF range.
16) FINAL PROBABILISTIC CONCLUSION
Базовый контекст: восходящая 1D, цена в premium 4H. Bullish 60% / Bearish 40%.
Sweep-reclaim long: снятие 98 → возврат → цели 104, 106 → инвалидация 97.
{"bull": 6, "bear": 4, "trap": 3, "p_bull": 60, "p_bear": 40, "setups": [{"name": "Sweep-reclaim long", "side": "long", "entry": 100, "stop": 98, "targets": [104, 106]}, {"name": "Breakdown short", "side": "short", "entry": 97, "stop": 99, "targets": [95]}]}"""


# ── Настройки ───────────────────────────────────────────────────────────────

class TestSettings:
    def test_clean_keeps_pool_pairs_once_and_bounded(self):
        out = llm_daily.clean({'pairs': ['solusdt', 'SOLUSDT', 'FAKEUSDT', 'ETHUSDT']}, llm_daily.defaults())
        assert out['pairs'] == ['SOLUSDT', 'ETHUSDT']
        many = llm_daily.clean({'pairs': list(llm_daily.ALLOWED)})
        assert len(many['pairs']) == llm_daily.MAX_PAIRS

    def test_clean_rejects_bad_hours(self):
        base = llm_daily.defaults()
        for bad in (24, -1, 5.5, 'утро', None, True):
            assert llm_daily.clean({'hour_utc': bad}, base)['hour_utc'] == 6, bad
        assert llm_daily.clean({'hour_utc': '7'}, base)['hour_utc'] == 7

    def test_settings_store_section_and_history(self):
        from accounts import settings_store
        data = settings_store.load(force=True)
        assert data[settings_store.DAILY] == {'enabled': True, 'pairs': ['BTCUSDT', 'ETHUSDT'], 'hour_utc': 6}
        assert data[settings_store.NOTIFY]['asset_report_telegram'] is True
        saved = settings_store.save({settings_store.DAILY: {'pairs': ['SOLUSDT', 'BTCUSDT'], 'hour_utc': 7}})
        assert saved['DAILY'] == {'enabled': True, 'pairs': ['SOLUSDT', 'BTCUSDT'], 'hour_utc': 7}
        changed = {c['field'] for c in settings_store.history()[0]['changes']}
        assert {'DAILY.pairs', 'DAILY.hour_utc'} <= changed
        assert settings_store.save({'DAILY': {'hour_utc': 30}})['DAILY']['hour_utc'] == 7, 'негодный час не пишется'


# ── Расписание ──────────────────────────────────────────────────────────────

class TestSchedule:
    def test_one_pair_per_hour_last_before_the_report_hour(self):
        assert llm_daily.slot_hours(['BTCUSDT', 'ETHUSDT'], 6) == {'BTCUSDT': 4, 'ETHUSDT': 5}
        assert llm_daily.slot_hours(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'], 1) == {'BTCUSDT': 0, 'ETHUSDT': 0, 'SOLUSDT': 0}

    def test_due_order(self, daily):
        assert llm_daily.due(CFG, at(3)) is None, 'до слота'
        assert llm_daily.due(CFG, at(4)) == ('BTCUSDT', '2026-10-15')
        llm_daily._save(llm_daily.report_path('2026-10-15', 'BTCUSDT'), {'day': '2026-10-15', 'answer': 'x'})
        assert llm_daily.due(CFG, at(4)) is None
        assert llm_daily.due(CFG, at(5)) == ('ETHUSDT', '2026-10-15')
        assert llm_daily.due(CFG, at(5, minute=40)) is None, 'вне окна сбора данных'
        assert llm_daily.due(CFG, at(4, day=16)) == ('BTCUSDT', '2026-10-16'), 'новый день — заново'

    def test_off_or_no_model(self, daily, monkeypatch):
        assert llm_daily.due({**CFG, 'enabled': False}, at(5)) is None
        assert llm_daily.due({**CFG, 'pairs': []}, at(5)) is None
        assert llm_daily.due(None, at(5)) is None
        monkeypatch.setattr(llm_daily.config, 'LLM_SERVER_URL', '')
        assert llm_daily.due(CFG, at(5)) is None

    def test_failure_is_retried_once(self, daily):
        path = llm_daily.report_path('2026-10-15', 'BTCUSDT')
        llm_daily._save(path, {'day': '2026-10-15', 'error': 'модель упала', 'attempts': 1})
        assert llm_daily.due(CFG, at(5)) == ('BTCUSDT', '2026-10-15')
        llm_daily._save(path, {'day': '2026-10-15', 'error': 'модель упала', 'attempts': 2})
        assert llm_daily.due(CFG, at(5)) == ('ETHUSDT', '2026-10-15')


# ── Структура по таймфреймам ────────────────────────────────────────────────

def frame(closes, spread=0.5):
    return pd.DataFrame({'open': closes, 'high': [c + spread for c in closes],
                         'low': [c - spread for c in closes], 'close': closes})


UP = [10, 11, 12, 13, 12, 11, 12, 13, 14, 15, 14, 13, 14, 15, 16, 17, 16, 15, 16, 17, 18]


class TestStructure:
    def test_swings_and_breaks(self):
        df = frame(UP)
        h, l, c = llm_daily._rows(df)
        hs, ls, trend = llm_daily.swing_labels(h, l)
        assert [p for _, p, _ in hs] == [13.5, 15.5, 17.5] and [x for _, _, x in hs] == ['—', 'HH', 'HH']
        assert [p for _, p, _ in ls] == [10.5, 12.5, 14.5] and trend == 'восходящая (HH + HL)'
        assert llm_daily.last_break(h, l, c) == (20, 'BOS', 'вверх', 17.5)
        assert llm_daily.last_break(h[:9], l[:9], c[:9]) == (8, 'CHoCH', 'вверх', 13.5), 'первый слом — CHoCH'

    def test_mtf_block_numbers(self):
        text = llm_daily.mtf_block(frame(UP), frame(UP), frame(UP), 17.0,
                                   {'usdt_d': 5.12, 'usdt_d_24h': -0.05, 'usdt_d_7d': 0.2})
        assert '1D: структура восходящая (HH + HL)' in text
        assert '4H: последний слом — BOS вверх за 17.5, 0 баров назад.' in text
        assert 'верх 17.5, низ 14.5, equilibrium 16; цена 17 — 83% от низа (premium)' in text
        assert 'USDT.D: 5.12%, за 24ч -0.05 п.п., за 7д +0.20 п.п.' in text

    def test_missing_data_is_said(self):
        text = llm_daily.mtf_block(None, None, None, 100.0, {'usdt_d': 5.0, 'stale': True})
        for part in ('1D: данных нет.', '4H: данных нет.', '15m: данных нет.', 'USDT.D: данных нет.'):
            assert part in text
        assert llm_daily.mood_text({}) == 'НАСТРОЕНИЕ РЫНКА: данных нет.'

    def test_trading_ai_rules_are_not_in_the_question(self):
        """Разметка ИИ-стратегии несёт её правила (законные стопы, минимальный стоп, её счёт) —
        разбор дня идёт по методике владельца, чужие правила ему не навязываются."""
        src = '\n'.join([
            'Пара: BTCUSDT   Цена: 100',
            'Минимальный стоп: 0.80% (издержки 0.30%)   Отступ стопа за уровень: 0.15% (ставит код)',
            '',
            'УРОВНИ (сверху вниз, расстояние от цены; «← СТОП» — стоп можно только за такой уровень; объём — к медиане)',
            '  ГОДЯТСЯ ПОД ВХОД: лонг — L1–L3   шорт — —',
            '  L1         101.5    +1.50%   пивот дня, касаний 3   ← СТОП: ШОРТ',
            '  L2            99    -1.00%   ордер-блок   ← СТОП: ЛОНГ/ШОРТ',
            '',
            'ТВОЙ СЧЁТ (твои прошлые планы; успех = цель раньше стопа)',
            '  всего 3/10'])
        out = llm_daily.context_text(src)
        for gone in ('Минимальный стоп', 'ГОДЯТСЯ', '← СТОП', 'ТВОЙ СЧЁТ', 'всего 3/10', 'стоп можно только'):
            assert gone not in out, gone
        assert 'пивот дня, касаний 3' in out and 'ордер-блок' in out and 'УРОВНИ (сверху вниз' in out


# ── Ответ модели ────────────────────────────────────────────────────────────

class TestAnswer:
    def test_parse_takes_the_last_json(self):
        p = llm_daily.parse(ANSWER)
        assert p['bull'] == 6 and len(p['setups']) == 2
        assert llm_daily.parse('без json') is None
        assert llm_daily.parse('{"setups": [ сломано ]}') is None
        fenced = 'Вывод.\n```json\n{"bull": 5, "bear": 5, "setups": [{"name": "a", "targets": [1]}]}\n```'
        assert llm_daily.parse(fenced)['bull'] == 5 and llm_daily.strip_json(fenced) == 'Вывод.'

    def test_rr_is_recomputed_by_code(self):
        rows = llm_daily.rr_table(llm_daily.parse(ANSWER))
        assert rows[0]['rr'] == [2.0, 3.0] and rows[0]['weak'] is False and rows[0]['side'] == 'long'
        assert rows[1]['rr'] == [1.0] and rows[1]['weak'] is True, 'R:R < 1.5 до первой цели — слабый'
        wrong = llm_daily.rr_table({'setups': [{'name': 'x', 'side': 'long', 'entry': 100, 'stop': 101, 'targets': [105]},
                                               {'name': 'y', 'side': 'short', 'entry': 100, 'stop': 102, 'targets': [103]},
                                               {'name': 'битый', 'side': 'long', 'entry': 'сто'}]})
        assert len(wrong) == 2
        assert wrong[0]['why'] == 'стоп не по ту сторону входа' and wrong[0]['weak'] and wrong[0]['rr'] == []
        assert wrong[1]['why'] == 'цель не по ту сторону входа'

    def test_summary_is_escaped_and_short(self):
        record = {'pair': 'BTCUSDT', 'price': 100.0, 'answer': ANSWER.replace('Базовый', '<b>Базовый</b> & <script>'),
                  'parsed': llm_daily.parse(ANSWER)}
        record['rr'] = llm_daily.rr_table(record['parsed'])
        text = llm_daily.summary(record)
        assert '&lt;script&gt;' in text and '<script>' not in text
        assert 'Анализ дня · BTC' in text and 'Sweep-reclaim long' in text and 'R:R 2 / 3' in text
        assert 'R:R 1 — слабый' in text and 'bull 6/10' in text and '"setups"' not in text
        assert 'MARKET REGIME' not in text, 'в выжимке — вывод (раздел 16), не весь разбор'
        long = dict(record, answer='16) FINAL PROBABILISTIC CONCLUSION\n' + 'х' * 9000)
        assert len(llm_daily.summary(long)) <= 4000

    def test_question_fits_the_window(self):
        q = 'ДАННЫЕ\n\nПОСЛЕДНИЕ 8 ЧАСОВЫХ СВЕЧЕЙ (старые → новые)\n  15.10 01:00 ▲+0.10%\n  15.10 02:00 ▼-0.20%\n\nРАССТАНОВКА'
        assert llm_daily.fit(q, count=lambda t: 5000, ctx=12288) == (q, llm_daily.MAX_TOKENS)

        def heavy(t):
            return 11000 if 'ЧАСОВЫХ СВЕЧЕЙ' in t else 9000
        shorter, n = llm_daily.fit(q, count=heavy, ctx=12288)
        assert 'опущены' in shorter and '15.10 01:00' not in shorter and 'РАССТАНОВКА' in shorter
        assert n == 12288 - 9000 - 64
        with pytest.raises(RuntimeError, match='не помещается'):
            llm_daily.fit(q, count=lambda t: 11000, ctx=12288)


# ── Поток разбора ───────────────────────────────────────────────────────────

class TestRun:
    def test_report_saved_and_summary_sent(self, daily, monkeypatch):
        sent = []
        monkeypatch.setitem(outbox._receivers, 'telegram', lambda name, *a, **k: sent.append((name, a)))
        llm_daily._job['pair'] = 'BTCUSDT'
        rec = llm_daily.run('BTCUSDT', '2026-10-15', 'Q', 100.0, ask=lambda q: (ANSWER, 1500.0, q + '!'),
                            idle=lambda: True, clock=lambda: at(4, 40))
        saved = json.load(open(llm_daily.report_path('2026-10-15', 'BTCUSDT'), encoding='utf-8'))
        assert saved['question'] == 'Q!' and saved['parsed']['bear'] == 4 and saved['rr'][0]['rr'] == [2.0, 3.0]
        assert rec['seconds'] == 1500.0 and llm_daily._job['pair'] is None
        assert sent and sent[0][0] == 'asset_report' and 'Анализ дня · BTC' in sent[0][1][0]

    def test_busy_model_moves_to_next_hour(self, daily):
        """Модель занята тетрадью или обзором до LATEST_START — отчёта нет, следующий час повторит."""
        waits = []
        rec = llm_daily.run('BTCUSDT', '2026-10-15', 'Q', 100.0, ask=lambda q: pytest.fail('спросил занятую модель'),
                            idle=lambda: False, clock=lambda: at(4, llm_daily.LATEST_START), pause=waits.append)
        assert rec is None and not os.path.exists(llm_daily.report_path('2026-10-15', 'BTCUSDT'))
        assert llm_daily.due(CFG, at(5)) == ('BTCUSDT', '2026-10-15')

    def test_failures_are_counted(self, daily):
        def boom(q):
            raise RuntimeError('модель упала')
        for n in (1, 2):
            assert llm_daily.run('BTCUSDT', '2026-10-15', 'Q', 100.0, ask=boom, idle=lambda: True,
                                 clock=lambda: at(4, 20), notify=False) is None
            saved = json.load(open(llm_daily.report_path('2026-10-15', 'BTCUSDT'), encoding='utf-8'))
            assert saved['attempts'] == n and 'модель упала' in saved['error']
        llm_daily.run('ETHUSDT', '2026-10-15', 'Q', 1.0, ask=lambda q: ('  ', 1.0, q), idle=lambda: True,
                      clock=lambda: at(5, 20), notify=False)
        assert 'пустой ответ' in json.load(open(llm_daily.report_path('2026-10-15', 'ETHUSDT'), encoding='utf-8'))['error']

    def test_tick_collects_in_the_cycle_and_hands_over(self, daily, monkeypatch):
        calls = []
        monkeypatch.setattr(llm_daily, 'collect', lambda pair, client=None, now=None: ('Q ' + pair, 100.0))

        def fake_run(pair, day, question, price):
            calls.append((pair, day, question, price))
            llm_daily._job['pair'] = None
        monkeypatch.setattr(llm_daily, 'run', fake_run)
        assert llm_daily.tick(client=object(), cfg=CFG, now=at(4)) is True
        llm_daily._job['thread'].join(5)
        assert calls == [('BTCUSDT', '2026-10-15', 'Q BTCUSDT', 100.0)]

    def test_tick_records_missing_data(self, daily, monkeypatch):
        monkeypatch.setattr(llm_daily, 'collect', lambda pair, client=None, now=None: (None, 'мало часовых свечей'))
        assert llm_daily.tick(cfg=CFG, now=at(4)) is False
        saved = json.load(open(llm_daily.report_path('2026-10-15', 'BTCUSDT'), encoding='utf-8'))
        assert saved['error'] == 'мало часовых свечей' and saved['attempts'] == 1

        def broken(pair, client=None, now=None):
            raise ValueError('биржа молчит')
        monkeypatch.setattr(llm_daily, 'collect', broken)
        assert llm_daily.tick(cfg=CFG, now=at(5)) is False
        assert 'биржа молчит' in json.load(open(llm_daily.report_path('2026-10-15', 'BTCUSDT'), encoding='utf-8'))['error']

    def test_one_job_at_a_time(self, daily):
        llm_daily._job['pair'] = 'BTCUSDT'
        assert llm_daily.tick(cfg=CFG, now=at(5)) is False
        assert llm_daily.busy() == 'BTCUSDT'


class TestPanel:
    def test_recent_for_the_panel(self, daily):
        for day, pair in (('2026-10-14', 'BTCUSDT'), ('2026-10-15', 'BTCUSDT'), ('2026-10-15', 'ETHUSDT')):
            llm_daily._save(llm_daily.report_path(day, pair), {'day': day, 'pair': pair, 'question': 'Q', 'answer': ANSWER})
        rows = llm_daily.recent()
        assert [(r['day'], r['pair']) for r in rows] == [('2026-10-15', 'ETHUSDT'), ('2026-10-15', 'BTCUSDT'),
                                                         ('2026-10-14', 'BTCUSDT')]
        assert 'question' not in rows[0] and '"setups"' not in rows[0]['answer']
        assert rows[0]['conclusion'].startswith('Базовый контекст') and 'MARKET REGIME' not in rows[0]['conclusion']
        assert [r['day'] for r in llm_daily.recent(days=1)] == ['2026-10-15', '2026-10-15']

    def test_dashboard_payload(self, daily):
        from control import dashboard
        data = dashboard.daily_payload()
        assert data['settings']['pairs'] == ['BTCUSDT', 'ETHUSDT'] and 'SOLUSDT' in data['allowed']
        assert data['reports'] == [] and data['busy'] is None and data['max_pairs'] == llm_daily.MAX_PAIRS

    def test_telegram_has_its_own_switch(self, monkeypatch):
        from accounts import settings_store
        from control import telegram_notify
        sent = []
        monkeypatch.setattr(telegram_notify, '_send', lambda text, *a, **k: sent.append(text) or True)
        assert telegram_notify.asset_report('🗓 <b>Анализ дня · BTC</b>') is True
        settings_store.save({'NOTIFY': {'asset_report_telegram': False}})
        assert telegram_notify.asset_report('🗓 <b>Анализ дня · ETH</b>') is False
        assert sent == ['🗓 <b>Анализ дня · BTC</b>']


class TestRules:
    def test_docs_export_matches_the_prompt(self):
        """Правило проекта: промт меняется только с экспортом (docs/Промт_ИИ_анализ_дня.txt)."""
        if not os.path.isdir(os.path.dirname(llm_daily.EXPORT)):
            pytest.skip('нет docs/')
        assert open(llm_daily.EXPORT, encoding='utf-8').read() == llm_daily.export_text(), \
            'промт анализа дня изменён без экспорта: python -m strategies.llm.llm_daily export'

    def test_owner_prompt_sections_are_all_there(self):
        for n, name in enumerate(('MARKET REGIME', 'HTF STRUCTURE', 'EMOTIONAL STATE', 'POSITIONING', 'LIQUIDITY MAP',
                                  'ALGO BEHAVIOR', 'NARRATIVE CONTEXT', 'CONTINUATION VS EXHAUSTION',
                                  'BULLISH SCENARIO', 'BEARISH SCENARIO', 'INVALIDATION LEVELS',
                                  'HIGH-PROBABILITY LIQUIDITY TARGETS', 'SCORING', 'EXECUTION QUALITY',
                                  'RISK CONDITIONS', 'FINAL PROBABILISTIC CONCLUSION'), start=1):
            assert f'{n}) {name}' in llm_daily.SYSTEM

    def test_no_accounts_and_no_other_strategy(self):
        from test_strategy_isolation import modules_of, PACKAGES
        path = os.path.join(HERE, 'strategies', 'llm', 'llm_daily.py')
        assert not modules_of(path) & set(PACKAGES)
        text = open(path, encoding='utf-8').read()
        for name in ('accounts', 'control', 'execution', 'strategies.fibo', 'strategies.smc', 'strategies.smcs'):
            assert f'from {name}' not in text and f'import {name}' not in text, name

    def test_bot_hook(self, monkeypatch):
        import bot
        src = open(os.path.join(HERE, 'bot.py'), encoding='utf-8').read()
        assert src.count('_daily_report(broker.client)') == 2, 'в конце цикла и на паузе'
        monkeypatch.setattr(llm_daily, 'tick', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('сбой')))
        bot._daily_report(None)                       # не бросает
