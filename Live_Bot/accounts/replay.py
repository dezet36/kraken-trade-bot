"""
Прогон сетапов на истории тем же кодом, что торгует, — для стендов
(реорганизация, этап 9: «стенд = бот по построению»).

Сетапы стратегии (в форме accounts/books.setup_copy) с моментами постановки и
5-минутные свечи по парам проходят через счёт «по инструкциям»
(accounts/manual.py): решение счёта — books.decide (риск от капитала,
кулдаун, предел издержек, смещение лимита, срок заявки стратегии —
strategy_profile), жизнь заявки и позиции — ядро execution/core.py. Ровно то,
что делает бумажный брокер со сделкой стратегии; совпадение со сделками
брокера держит tests/test_replay_matches_broker.py.

Чего здесь нет — того, что у брокера общее на все стратегии: термостат
портфеля и пределы портфеля из настроек. Стенд меряет стратегию на её
собственном счёте.

Только для стендов и проверок: прогон подменяет каталог данных на время
работы (свой временный) и не должен вызываться внутри бота.
"""

import contextlib
import tempfile

STEP_MS = 3_600_000           # шаг «цикла бота» по умолчанию — час, как в эталоне брокера


class Candles:
    """Свечи прогона: отдаёт все — отбор «закрыта к этому моменту» делает books.bars."""

    def __init__(self, candles):
        self.candles = candles

    def fetch_ohlcv(self, pair, timeframe, since=None, limit=None):
        rows = [c for c in self.candles.get(pair, []) if since is None or c[0] >= since]
        return rows[:limit] if limit else rows

    def fetch_funding_rate(self, pair):
        raise RuntimeError('в прогоне ставка — типовая из настроек')


@contextlib.contextmanager
def _data_dir(path):
    """Каталог данных прогона и чистые кэши счетов на время работы."""
    from infra import config
    from accounts import books, live
    saved = (config.DATA_DIR, dict(books._cache), dict(live._cache), books._notify, list(books._outbox))
    config.DATA_DIR = path
    books._cache.update(path=None, state=None)
    live._cache.update(key=None, data=None)
    books._notify = None
    try:
        yield
    finally:
        config.DATA_DIR = saved[0]
        books._cache.clear()
        books._cache.update(saved[1])
        live._cache.clear()
        live._cache.update(saved[2])
        books._notify = saved[3]
        books._outbox[:] = saved[4]


def run(setups, candles, *, deposit=10_000.0, risk_pct=None, step_ms=STEP_MS, until=None,
        offer_delay_ms=0, data_dir=None):
    """
    setups: [(момент_мс, стратегия, сетап)] — сетап в форме books.setup_copy;
    candles: {пара: [[мс, open, high, low, close, volume], ...]} 5-минутные.
    Каждая стратегия торгует на своём счёте (deposit, risk_pct — по умолчанию
    стандарт теста config.RISK_PER_TRADE). Шаг цикла — step_ms: сначала
    ведение (налив, стопы, цели), потом новые сетапы — как в цикле бота.
    Возвращает {'trades': строки журнала, 'books': {стратегия: книга}}.
    """
    from infra import config
    from accounts import books, live, manual
    setups = sorted(setups, key=lambda s: s[0])
    if not setups:
        return {'trades': [], 'books': {}}
    risk = float(config.RISK_PER_TRADE if risk_pct is None else risk_pct)
    with contextlib.ExitStack() as stack:
        path = data_dir or stack.enter_context(tempfile.TemporaryDirectory(prefix='replay-'))
        stack.enter_context(_data_dir(path))
        codes = {}
        for strategy in dict.fromkeys(s[1] for s in setups):
            code, _ = live.save({'name': f'Прогон {strategy}', 'kind': 'manual', 'enabled': True,
                                 'strategies': [strategy], 'deposit': deposit, 'risk_pct': risk,
                                 'profit_target_pct': 0, 'max_drawdown_pct': 0, 'daily_loss_pct': 0,
                                 'draft': False})
            codes[strategy] = code
        client = Candles(candles)
        last_bar = max((rows[-1][0] for rows in candles.values() if rows), default=setups[-1][0])
        end = until or last_bar + 2 * books.BAR_MS
        now = setups[0][0]           # шаги цикла — от первого сетапа, его же фазой
        k = 0
        while now <= end:
            manual.update(client, now_ms=now)
            while k < len(setups) and setups[k][0] <= now:
                when, strategy, setup = setups[k]
                manual.offer(strategy, setup, now_ms=now + offer_delay_ms)
                k += 1
            now += step_ms
        trades = books.read_journal()
        state = books.state()
        return {'trades': trades,
                'books': {s: state.get(c) for s, c in codes.items()}}
