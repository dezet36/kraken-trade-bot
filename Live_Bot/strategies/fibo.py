"""
Адаптер старой ФИБО (импульс часа + откат по Фибоначчи) под единый набор
функций реестра стратегий (strategies/registry.py).

Сама стратегия пока живёт там же, где жила: сканер — pair_scanner.py, сетап —
strategy.py, параметры — config.py (раздел «Стратегия Фибо-лимит» и соседние).
Здесь — только то, что раньше было разбросано ветками по общим модулям: цикл
бота (сканирование, блок-лист часов), сборка сигнала (bot._build_signal),
величины исполнения (strategy_profile), разметка графика (setup_geometry).
Поведение не меняется — его держат эталоны tests/golden/.
"""

from datetime import datetime, timezone

from logger import log

NAME = 'FIBO'


def scan(pairs, gate, client=None):
    """Кандидаты сканера ФИБО. Блок-лист часов входа — её собственный фильтр
    (откалиброван под ФИБО; сейчас пуст)."""
    import config
    hour = datetime.now(timezone.utc).hour
    if hour in config.BLOCK_ENTRY_HOURS_UTC:
        log(f"   FIBO: {hour:02d}:xx UTC в блок-листе, пропускаем")
        return []
    import pair_scanner
    return pair_scanner.scan_for_setups(pairs, gate, client=client)


def build_signal(candidate):
    """Сигнал по кандидату сканера: сетап считает strategy.analyze_market."""
    from strategy import analyze_market
    pair = candidate['pair']
    signal = analyze_market(candidate['df_1h'], None, pair)
    if not signal:
        return None, None
    signal['htf_trend'] = candidate.get('htf_trend', 'NEUTRAL')
    # funding_bp — ставка в момент решения: сканер кладёт её в кандидата,
    # чтобы фильтр толпы проверялся вживую, а до 01.10.2026 она здесь
    # отбрасывалась и до журнала не доходила.
    signal['scan'] = {k: candidate.get(k) for k in
                      ('score', 'score_legacy', 'rr_est', 'htf_strength',
                       'proximity', 'size_pct', 'funding_bp')}
    log(f"\n[FIBO] {pair}: зона {candidate.get('zone')}, "
        f"HTF {signal['htf_trend']}")
    return signal, candidate['df_1h']


def profile():
    """Величины исполнения. Остальное у ФИБО — общие из config (они и
    считались для неё). config — заново при каждом вызове: тесты его
    перезагружают."""
    import config
    return {'fills_through_market': getattr(config, 'FIBO_FILL_THROUGH_MARKET', False)}


def geometry(signal, g):
    """Зона A (там стоит лимит), зона B (граница инвалидации) и импульс."""
    import config
    za, zb = signal.get('zone_a') or {}, signal.get('zone_b') or {}

    # Границы в подписи берутся ИЗ КОНФИГА, а не пишутся руками. Написанная
    # руками подпись зоны B утверждала «61.8–88.6%», тогда как зона стоит на
    # 78.6–88.6%: на графике всё было нарисовано правильно, а прочитать с него
    # можно было неверное число.
    def _pct(value):
        return f'{value * 100:.1f}'.rstrip('0').rstrip('.')

    g.band(za.get('bottom'), za.get('top'),
           f'зона A · {_pct(config.ZONE_A_BOTTOM)}–{_pct(config.ZONE_A_TOP)}%', main=True)
    g.band(zb.get('bottom'), zb.get('top'),
           f'зона B · {_pct(config.ZONE_B_BOTTOM)}–{_pct(config.ZONE_B_TOP)}%')
    g.leg('начало импульса', 'конец импульса')
