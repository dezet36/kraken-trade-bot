"""
Открытый интерес выравнивается ПО МЕТКЕ ВРЕМЕНИ и не заглядывает вперёд.

ЗАЧЕМ ЭТИ ПРОВЕРКИ. В истории интереса бывают пропуски часов. Совмещение
рядов по порядку строк при пропуске сдвигает весь остаток ряда вверх — и
признак начинает содержать будущее, оставаясь при этом правдоподобным на вид.
Такая ошибка не падает и не выдаёт себя ничем, кроме неправдоподобно хорошего
результата; в этом проекте она уже стоила одного ложного «+0.610 R из ничего».
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'research'))

break_diagnosis = pytest.importorskip('break_diagnosis')


def _write(tmp_path, pair, stamps, values):
    folder = tmp_path / 'open_interest'
    folder.mkdir(exist_ok=True)
    pd.DataFrame({'timestamp': stamps, 'open_interest': values}).to_csv(
        folder / f'{pair}_1h.csv', index=False)


def test_missing_hours_leave_gaps_not_shifts(tmp_path):
    """Пропущенный час обязан стать NaN, а не сдвинуть весь остаток ряда."""
    bars = pd.date_range('2024-01-01', periods=6, freq='h', tz='UTC')
    # Третьего часа в истории интереса нет.
    present = bars.delete(2)
    _write(tmp_path, 'BTCUSDT', present, [10.0, 11.0, 13.0, 14.0, 15.0])

    naive = bars.tz_localize(None)
    out = break_diagnosis.load_oi(str(tmp_path), 'BTCUSDT', pd.Series(naive))

    assert np.isnan(out[2]), 'пропущенный час должен остаться пустым'
    # Значения после пропуска стоят на своих местах, а не подтянулись.
    assert out[3] == 13.0
    assert out[5] == 15.0


def test_values_land_on_their_own_timestamps(tmp_path):
    """Значение часа T оказывается ровно на баре T, без смещения."""
    bars = pd.date_range('2024-03-05 07:00', periods=4, freq='h', tz='UTC')
    _write(tmp_path, 'ETHUSDT', bars, [100.0, 200.0, 300.0, 400.0])

    out = break_diagnosis.load_oi(str(tmp_path), 'ETHUSDT',
                                  pd.Series(bars.tz_localize(None)))
    assert list(out) == [100.0, 200.0, 300.0, 400.0]


def test_unknown_pair_gives_none(tmp_path):
    """Пары без истории пропускаются, а не роняют прогон."""
    assert break_diagnosis.load_oi(str(tmp_path), 'NOPEUSDT',
                                   pd.Series(pd.to_datetime([]))) is None


def test_change_windows_never_use_the_breakout_hour():
    """
    Оба окна изменения интереса кончаются на НАЧАЛЕ пробойного часа.

    Вход идёт по закрытию этого часа, а снимок с меткой T — состояние на его
    начало. Сдвиги в замере равны 24 и 1, то есть признак опирается на
    прошлое; сдвиг 0 означал бы использование самого пробойного часа.
    """
    source = os.path.join(ROOT, 'research', 'break_diagnosis.py')
    with open(source, encoding='utf-8') as handle:
        text = handle.read()

    assert 'shift(24)' in text
    assert 'shift(1)' in text
    assert 'shift(-' not in text, 'отрицательный сдвиг — это взгляд в будущее'


def test_forward_return_flips_for_shorts_but_open_interest_does_not():
    """
    Ход цены разворачивается по стороне, интерес — нет, и это не описка.

    Рост интереса на пробое вверх означает новые лонги, на пробое вниз — новые
    шорты. Оба подтверждают движение, поэтому переворот знака у интереса погасил
    бы ровно тот эффект, который ищется.
    """
    source = os.path.join(ROOT, 'research', 'break_diagnosis.py')
    with open(source, encoding='utf-8') as handle:
        text = handle.read()

    assert "'forward': ahead if up else -ahead" in text
    assert "'oi_day': oi_day[i]" in text
    assert "'oi_day': oi_day[i] if up else -oi_day[i]" not in text
