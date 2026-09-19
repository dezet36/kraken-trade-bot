"""
Каким таймфреймом рисовать план модели для человека.

План строится по часовым свечам, но картинка в Telegram — не для модели, а
для того, кто на неё смотрит с телефона. Ей нужно одно: чтобы вход, стоп и
цели читались, а свечи не сжимались в полоску. Часовые свечи за пять суток
это не гарантировали: план с входом в четырёх процентах от цены и целью ещё
в десяти умещал свечи в нижнюю треть картинки, а план, у которого всё в двух
процентах вокруг цены, наоборот, терялся среди месячного размаха.

Правило простое и объяснимое. Всё, что план хочет показать — вход, стоп,
цели, уровень условия и текущая цена, — измеряется в процентах от цены:
    • план близко и узок (вход не дальше NEAR_PCT, размах не шире TIGHT_PCT)
      — четвертьчасовые свечи за двое суток: видно, как цена подходит;
    • иначе — четырёхчасовые за месяц: видно, откуда цена придёт к входу и
      через что пройдёт к цели.
Часовые свечи остались запасным вариантом — когда биржа не отдала выбранный
таймфрейм, а часовые уже на руках.
"""

# Свечей в окне на каждом таймфрейме: 15m — двое суток, 4h — месяц,
# 1h — пять суток (прежнее окно).
FRAMES = {'15m': 192, '4h': 180, '1h': 120}
SPAN_TEXT = {'15m': '15-минутные свечи, двое суток',
             '4h': '4-часовые свечи, месяц',
             '1h': 'часовые свечи, пять суток'}

NEAR_PCT = 1.5      # вход не дальше этого от цены — «близко»
TIGHT_PCT = 6.0     # весь план в таком размахе — «узко»


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
    -> (таймфрейм, число свечей). Без цены решает по одному размаху плана.
    """
    points = plan_points(signal, price)
    entry = float((signal.get('params') or {}).get('entry') or 0)
    if not points or not entry:
        return '1h', FRAMES['1h']
    base = float(price or entry)
    span = (max(points) - min(points)) / base * 100
    distance = abs(base - entry) / base * 100 if price else 0.0
    tf = '15m' if (distance <= NEAR_PCT and span <= TIGHT_PCT) else '4h'
    return tf, FRAMES[tf]


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
