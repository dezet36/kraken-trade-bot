"""
Каким таймфреймом рисовать план модели для человека.

ГЛАВНОЕ ПРАВИЛО: показываем ТОТ ТАЙМФРЕЙМ, НА КОТОРОМ ТОРГУЕМ. План
строится по часовым свечам — значит и картинка часовая. До 23.09.2026 здесь
выбиралось «удобнее для глаза»: четвертьчасовые для тесного плана, иначе
четырёхчасовые. Часовых в выборе не было вовсе, и человек видел на экране
один масштаб, а бот работал в другом: уровни плана на четырёхчасовом
графике лежат не там, где их видит стратегия, и проверить план глазами
нельзя.

Остаётся одна свобода — СКОЛЬКО часовых свечей показать. Окно берётся
таким, чтобы в него попали и вход, и стоп, и цели: у плана с целью в
десяти процентах пятисуточное окно оставляло цель за краем.
"""

# Рабочий таймфрейм — часовой; окно подбирается под размах плана.
WORKING_TF = '1h'
FRAMES = {'15m': 192, '4h': 180, '1h': 120}
SPAN_TEXT = {'15m': '15-минутные свечи, двое суток',
             '4h': '4-часовые свечи, месяц',
             '1h': 'часовые свечи',
             '1h_wide': 'часовые свечи, десять суток'}

# Размах плана, при котором пятисуточного окна мало: цель уезжает за край.
WIDE_PCT = 8.0
BARS_NORMAL = 120   # пять суток
BARS_WIDE = 240     # десять суток


def plan_points(signal, price=None):
    """Все цены, которые график обязан показать."""
    params = signal.get('params') or {}
    llm = signal.get('llm') or {}
    points = [params.get('entry'), params.get('stop_loss'), llm.get('trigger_level')]
    points += list(params.get('tp_targets') or [params.get('take_profit_1')])
    if price:
        points.append(price)
    return [float(p) for p in points if p]


def pick(signal, price=None):
    """
    -> (таймфрейм, число свечей). Таймфрейм всегда рабочий — часовой;
    решается только ширина окна.
    """
    points = plan_points(signal, price)
    entry = float((signal.get('params') or {}).get('entry') or 0)
    if not points or not entry:
        return WORKING_TF, BARS_NORMAL
    base = float(price or entry)
    span = (max(points) - min(points)) / base * 100
    return WORKING_TF, (BARS_WIDE if span > WIDE_PCT else BARS_NORMAL)


def fit_window(df, points, min_bars=60, ratio=3.0):
    """
    Подрезает окно слева, пока размах свечей не станет соразмерен плану.

    Месяц четырёхчасовых свечей у монеты, прошедшей за месяц 40%, сжал бы
    план в 8% в узкую полоску. Окно укорачивается, пока размах свечей не
    уложится в ratio размахов плана, но не короче min_bars: часть истории
    важнее идеальной пропорции.
    """
    if df is None or not points or len(df) <= min_bars:
        return df
    plan = max(points) - min(points)
    if plan <= 0:
        return df
    highs = df['High'] if 'High' in df.columns else df['high']
    lows = df['Low'] if 'Low' in df.columns else df['low']
    n = len(df)
    while n > min_bars:
        candles = float(highs.iloc[-n:].max()) - float(lows.iloc[-n:].min())
        if candles <= plan * ratio:
            break
        n -= max(1, n // 10)
    n = max(n, min_bars)
    return df.iloc[-n:] if n < len(df) else df
