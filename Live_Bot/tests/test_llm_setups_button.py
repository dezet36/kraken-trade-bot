"""
Кнопка «Сетапы ИИ» в Telegram: по нажатию приходят живые сетапы — планы,
ждущие условия, заявки, ждущие цену, и открытые позиции — то, что ещё
может сбыться. Каждый с входом, стопом, целью, условием и сроком жизни.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_llm          # noqa: E402
import telegram_notify as tg  # noqa: E402


def approving_verdict(**over):
    out = {
        'ok': True, 'gate': '', 'detail': '',
        'side': 'LONG', 'entry': 100.0, 'stop': 97.0,
        'targets': [107.0, 109.0], 'inval': 97.0,
        'p': 0.58, 'rr': 2.33, 'ev': 0.9, 'cost_r': 0.025, 'votes': 4,
        'why': 'скопление снизу свежее', 'risk': 'слив ОИ', 'ids': {'entry': 'L5', 'stop': 'L7'},
    }
    out.update(over)
    return out


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    strategy_llm._armed.clear()
    monkeypatch.setattr(strategy_llm, '_ARMED_FILE', str(tmp_path / 'armed.json'))
    monkeypatch.setattr(strategy_llm, '_armed_loaded', True)


class FakeBroker:
    def snapshot(self):
        return {'open': [
            {'strategy': 'LLM', 'pair': 'AAVEUSDT', 'direction': 'SHORT', 'entry': 139.93, 'stop': 144.18,
             'tp1': 124.78, 'price': 138.5, 'unrealised_r': 0.34, 'why': 'вход из брейкера'},
            {'strategy': 'FIBO', 'pair': 'BTCUSDT', 'direction': 'LONG', 'entry': 1, 'stop': 0.9, 'tp1': 2},
        ], 'pending': [
            {'strategy': 'LLM', 'pair': 'BNBUSDT', 'direction': 'LONG', 'entry': 768.67, 'stop': 756.36,
             'tp1': 836.33, 'rr': 5.5, 'distance_pct': 1.2, 'waiting_min': 95, 'expires_in_min': 625,
             'why': 'откат к пивоту дня'},
        ]}


class TestLiveSetupsAreCollected:
    def test_armed_pending_and_open_of_the_ai_only(self):
        strategy_llm._arm('SOLUSDT', approving_verdict(trigger_when='sweep_reclaim', trigger_level=113.84,
                                                       trigger_id='L6'))
        got = strategy_llm.current_setups(FakeBroker(), now=time.time() + 30 * 60)
        assert [a['pair'] for a in got['armed']] == ['SOLUSDT']
        armed = got['armed'][0]
        assert armed['when'] == 'sweep_reclaim' and armed['level'] == 113.84
        assert armed['minutes'] == 30 and armed['left_min'] == 12 * 60 - 30
        assert [o['pair'] for o in got['pending']] == ['BNBUSDT']
        assert [o['pair'] for o in got['open']] == ['AAVEUSDT'], 'чужие стратегии не в счёт'

    def test_without_a_broker_only_plans(self):
        strategy_llm._arm('SOLUSDT', approving_verdict(trigger_when='retest', trigger_level=100.0, trigger_id='L3'))
        got = strategy_llm.current_setups(None)
        assert len(got['armed']) == 1 and got['pending'] == [] and got['open'] == []

    def test_a_broken_broker_does_not_hide_the_plans(self):
        class Broken:
            def snapshot(self):
                raise RuntimeError('файл занят')
        strategy_llm._arm('SOLUSDT', approving_verdict(trigger_when='retest', trigger_level=100.0, trigger_id='L3'))
        got = strategy_llm.current_setups(Broken())
        assert len(got['armed']) == 1


class TestTheListReadsLikeAPlan:
    def test_every_section_with_numbers_condition_and_lifetime(self):
        strategy_llm._arm('SOLUSDT', approving_verdict(trigger_when='sweep_reclaim', trigger_level=113.84,
                                                       trigger_id='L6'))
        text = tg.llm_setups_text(strategy_llm.current_setups(FakeBroker(), now=time.time() + 30 * 60))
        for piece in ('Живые сетапы ИИ', 'Ждут условия (1)', 'SOLUSDT LONG', 'вход', 'стоп', 'цель',
                      'вынос за 113.8400 и возврат не позже 3 свечей', 'ждёт 30м', 'снимется через 11ч 30м',
                      'Заявка стоит, ждёт цену (1)', 'BNBUSDT LONG', 'до лимита 1.2%', 'снимется через 10ч 25м',
                      'В позиции (1)', 'AAVEUSDT SHORT', '+0.34 R', 'откат к пивоту дня', 'вход из брейкера'):
            assert piece in text, piece
        order = ['Ждут условия', 'Заявка стоит', 'В позиции']
        positions = [text.index(piece) for piece in order]
        assert positions == sorted(positions)

    def test_nothing_alive_says_so(self):
        text = tg.llm_setups_text({'armed': [], 'pending': [], 'open': []})
        assert 'Живых сетапов ИИ нет' in text


class TestTheButtonAndCommandSendTheList:
    def test_the_command_sends_the_list_and_the_menu_has_the_button(self, monkeypatch):
        import telegram_bot
        sent = []
        ctl = telegram_bot.BotController()
        ctl.trade_manager = FakeBroker()
        monkeypatch.setattr(ctl, '_send', lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup)))
        ctl._handle_command('/setups', '1', [])
        assert sent and 'Живые сетапы ИИ' in sent[-1][0] and 'AAVEUSDT SHORT' in sent[-1][0]
        # Кнопка в меню статуса
        import inspect
        src = inspect.getsource(telegram_bot.BotController._send_status)
        assert '"/setups"' in src and 'Сетапы ИИ' in src
        assert '/setups' in inspect.getsource(telegram_bot.BotController._send_help)
        assert '"setups"' in inspect.getsource(telegram_bot.BotController._set_commands)
