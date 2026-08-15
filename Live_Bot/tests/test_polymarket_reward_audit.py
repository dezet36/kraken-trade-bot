"""
Сверка обещанной награды с выплаченной.

ПОЧЕМУ ЭТА ПРОВЕРКА ВООБЩЕ ПОЯВИЛАСЬ. Модель награды ошибалась дважды и оба раза
крупно: обещала $2.85 в сутки при выплаченных восьми центах, а по отдельному
рынку сулила $0.0011 там, где биржа заплатила $0.0767. Оба раза расхождение
находилось руками и не сразу — а решения по модели принимались сразу.

Площадка отдаёт точный ответ по дням. Значит проверять её можно вычитанием.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import datetime  # noqa: E402

from polymarket import reward_audit  # noqa: E402


class _Exchange:
    """Биржа, отвечающая заранее известными выплатами."""

    def __init__(self, by_day, fails=False):
        self.by_day = by_day
        self.fails = fails
        self.asked = []

    def get_earnings_for_user_for_day(self, day):
        self.asked.append(day)
        if self.fails:
            raise RuntimeError('сеть')
        return [{'earnings': v} for v in self.by_day.get(day, [])]


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _use_journal(tmp_path, monkeypatch):
    path = os.path.join(str(tmp_path), 'mm_reward_promise.jsonl')
    monkeypatch.setattr(reward_audit, 'JOURNAL', path)
    return path


class TestThePromiseIsWrittenDown:

    def test_a_plan_leaves_a_record(self, tmp_path, monkeypatch):
        path = _use_journal(tmp_path, monkeypatch)
        reward_audit.remember({'rewards_daily': 5.03, 'used': 39.06,
                               'markets': [1, 2, 3]})
        row = json.loads(open(path, encoding='utf-8').read().strip())
        assert row['promised_daily'] == 5.03
        assert row['used'] == 39.06
        assert row['markets'] == 3

    def test_a_day_averages_its_revisions(self, tmp_path, monkeypatch):
        """
        План пересматривается каждые несколько минут, и честное обещание за
        день — то, что стояло В СРЕДНЕМ, а не последнее перед полуночью.
        """
        _use_journal(tmp_path, monkeypatch)
        for value in (2.0, 4.0, 6.0):
            reward_audit.remember({'rewards_daily': value, 'used': 40})
        got = reward_audit.promises()
        assert got[_today().isoformat()] == 4.0

    def test_a_broken_journal_does_not_stop_the_plan(self, monkeypatch):
        """Журнал — удобство. Ронять из-за него раскладку нечего."""
        monkeypatch.setattr(reward_audit, 'JOURNAL',
                            os.path.join(os.devnull, 'нельзя', 'сюда.jsonl'))
        assert reward_audit.remember({'rewards_daily': 1.0}) is True


class TestTheReportSubtracts:

    def test_it_shows_how_badly_the_model_overstates(self, tmp_path,
                                                     monkeypatch):
        """
        Настоящий замер: обещали $2.85, заплатили $0.0808. Завышение в
        тридцать пять раз — ровно то, что искалось руками.
        """
        _use_journal(tmp_path, monkeypatch)
        day = (_today() - datetime.timedelta(days=1)).isoformat()
        with open(reward_audit.JOURNAL, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'day': day, 'promised_daily': 2.85}) + '\n')
        got = reward_audit.report(_Exchange({day: [0.004055, 0.076684, 0.00002]}))
        row = next(r for r in got['days'] if r['day'] == day)
        assert row['paid'] == 0.080759
        assert row['ratio'] == 35.3
        assert got['overstates'] == 35.3
        assert got['checked_days'] == 1

    def test_today_is_not_judged(self, tmp_path, monkeypatch):
        """
        Сутки ещё идут: выплата за сегодня всегда меньше обещанной просто
        поэтому. Сравнивать её значило бы каждый день видеть мнимый провал.
        """
        _use_journal(tmp_path, monkeypatch)
        reward_audit.remember({'rewards_daily': 5.0, 'used': 40})
        today = _today().isoformat()
        got = reward_audit.report(_Exchange({today: [0.01]}))
        row = next(r for r in got['days'] if r['day'] == today)
        assert row['closed'] is False
        assert 'ratio' not in row
        assert got['checked_days'] == 0

    def test_a_model_that_matches_shows_one(self, tmp_path, monkeypatch):
        _use_journal(tmp_path, monkeypatch)
        day = (_today() - datetime.timedelta(days=1)).isoformat()
        with open(reward_audit.JOURNAL, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'day': day, 'promised_daily': 0.5}) + '\n')
        got = reward_audit.report(_Exchange({day: [0.5]}))
        assert got['overstates'] == 1.0

    def test_silence_from_the_exchange_is_not_a_zero(self, tmp_path,
                                                    monkeypatch):
        """
        Биржа не ответила — это «не знаем», а не «не заплатили». Ноль здесь
        объявил бы модель негодной по причине обрыва связи.
        """
        _use_journal(tmp_path, monkeypatch)
        day = (_today() - datetime.timedelta(days=1)).isoformat()
        with open(reward_audit.JOURNAL, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'day': day, 'promised_daily': 2.0}) + '\n')
        got = reward_audit.report(_Exchange({}, fails=True))
        row = next(r for r in got['days'] if r['day'] == day)
        assert row['paid'] is None
        assert got['overstates'] is None

    def test_without_a_wallet_there_are_only_promises(self, tmp_path,
                                                     monkeypatch):
        _use_journal(tmp_path, monkeypatch)
        reward_audit.remember({'rewards_daily': 3.0, 'used': 40})
        got = reward_audit.report(None)
        assert got['checked_days'] == 0
        assert got['days']
