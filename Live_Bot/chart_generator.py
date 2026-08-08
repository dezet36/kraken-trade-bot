"""
Картинка сделки для Telegram: часовые свечи, план сделки и РАЗМЕТКА СЕТАПА.

Раньше здесь было ровно три линии — вход, стоп, цель — и панель RSI, одинаково
для всех четырёх стратегий. В шапке это называлось «лаконичный вид как в
TradingView», но лаконичность вышла боком: по такой картинке нельзя ответить на
единственный вопрос, ради которого её и смотрят — на чём стратегия сработала.
Ордер-блок, зона коррекции, уровень, пробитая структура — всё это оставалось за
кадром, и сообщение отличалось от сообщения соседней стратегии только числами.

Разметку считает общий модуль setup_geometry, тот же, что рисует график в
панели. Своей второй реализации здесь нет и быть не должно: разойдясь, они
показали бы разные зоны для одной сделки.

Что рисуется:
    • свечи 1H, тёмная тема;
    • зоны сетапа полосами, зона входа — ярче прочих;
    • уровни сетапа пунктиром с подписями;
    • план сделки: вход, стоп, цель;
    • панель RSI(14) с уровнями 70/30.
"""

import os
import time
import tempfile
import pandas as pd
import config
from logger import log


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-9)
    return (100 - 100 / (1 + rs)).fillna(50)


def _strategy_of(signal: dict) -> str:
    """
    Чья это сделка — по полям сигнала.

    Имени стратегии в сигнале нет: каждая кладёт свой раздел и тем себя и
    выдаёт. Порядок проверок значения не имеет — разделы не пересекаются.
    Неизвестный сигнал отдаёт пустую строку, и разметка получится пустой:
    график останется прежним, но не сломается.
    """
    for section, name in (('smc', 'SMC'), ('levels', 'LEVELS'),
                          ('rsibb', 'RSIBB')):
        if signal.get(section):
            return name
    if signal.get('zone_a') or signal.get('zone_b'):
        return 'FIBO'
    return ''


def generate_trade_chart(signal: dict, df_1h) -> str:
    """Чистый 1H-график с уровнями Entry/SL/TP и панелью RSI. Возвращает путь к PNG или None."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import mplfinance as mpf
    except ImportError:
        log("chart_generator: matplotlib/mplfinance не установлены — график пропущен")
        return None

    try:
        setup   = signal['setup']
        params  = signal['params']
        pair    = signal['trading_pair']
        is_long = setup['type'] == 'LONG'
        htf     = signal.get('htf_trend', '—')

        entry = float(params['entry'])
        sl    = float(params['stop_loss'])
        tp    = float(params['take_profit_1'])     # единственный тейк (уровень config.TP1_LEVEL)
        rr    = params.get('rr', 0)
        tp_pct = getattr(config, 'TP1_LEVEL', 0.25) * 100

        def _fp(p):
            if p >= 1000:   return f"{p:.2f}"
            if p >= 1:      return f"{p:.4f}"
            if p >= 0.01:   return f"{p:.6f}"
            if p >= 0.0001: return f"{p:.8f}"
            return f"{p:.10f}"

        # ── OHLCV ─────────────────────────────────────────────────────────────
        df = df_1h.copy().reset_index(drop=True)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.set_index('timestamp')[['open', 'high', 'low', 'close', 'volume']]
        df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']

        # Окно отображения: ~15 свечей до начала импульса (для контекста)
        try:
            a_idx = int(df.index.get_indexer([pd.Timestamp(setup['start_time'])], method='nearest')[0])
            start = max(0, a_idx - 15)
            if start > 0:
                df = df.iloc[start:].copy()
        except Exception:
            pass

        rsi = _rsi(df['Close'])

        # ── Разметка сетапа ───────────────────────────────────────────────────
        # Считает общий модуль — тот же, что и для графика в панели. Стратегию
        # он определяет сам по полям сигнала; здесь её имя не хранится.
        try:
            import setup_geometry
            geo = setup_geometry.build(_strategy_of(signal), signal) or {}
        except Exception as exc:                       # noqa: BLE001
            log(f"chart_generator: разметка не собралась — {exc}")
            geo = {}
        geo_bands = geo.get('bands') or []
        geo_lines = geo.get('lines') or []

        # ── Y-границы: свечи + уровни ─────────────────────────────────────────
        # РАЗМЕТКА ГРАНИЦЫ НЕ РАСТЯГИВАЕТ. Зона B у Фибоначчи и начало импульса
        # бывают далеко от цены, и ради них пришлось бы сжать все свечи в
        # полоску. Что не поместилось в окно — не рисуется.
        lo = min(float(df['Low'].min()), sl, entry, tp)
        hi = max(float(df['High'].max()), sl, entry, tp)
        pad = (hi - lo) * 0.05 or hi * 0.01
        y_min, y_max = lo - pad, hi + pad

        # ── Тёмный TradingView-стиль ──────────────────────────────────────────
        mc = mpf.make_marketcolors(
            up='#26A69A', down='#EF5350',
            wick={'up': '#26A69A', 'down': '#EF5350'},
            edge={'up': '#26A69A', 'down': '#EF5350'},
            volume='#2A2E39',
        )
        style = mpf.make_mpf_style(
            marketcolors=mc, gridstyle=':', gridcolor='#2A2E39',
            facecolor='#131722', edgecolor='#2A2E39', figcolor='#131722',
            rc={'axes.labelcolor': '#B2B5BE', 'xtick.color': '#B2B5BE',
                'ytick.color': '#B2B5BE', 'text.color': '#D1D4DC',
                'axes.titlecolor': '#FFFFFF'},
        )

        dir_icon = "▲ LONG" if is_long else "▼ SHORT"
        htf_str  = f"  HTF: {htf}" if htf not in ('—', 'NEUTRAL', None) else ""
        title    = f"{pair} · 1H  {dir_icon}{htf_str}"

        # RSI-панель (с уровнями 70/30)
        n = len(df)
        addplots = [
            mpf.make_addplot(rsi, panel=1, color='#B2B5BE', width=1.1, ylabel='RSI', ylim=(0, 100)),
            mpf.make_addplot([70] * n, panel=1, color='#555A66', width=0.7, linestyle='--'),
            mpf.make_addplot([30] * n, panel=1, color='#555A66', width=0.7, linestyle='--'),
        ]

        fig, axes = mpf.plot(
            df, type='candle', style=style, title=title, volume=False,
            addplot=addplots, panel_ratios=(3, 1), returnfig=True,
            figsize=(14, 8), tight_layout=True,
        )
        ax = axes[0]
        ax.set_ylim(y_min, y_max)
        xlim  = ax.get_xlim()
        x_rng = xlim[1] - xlim[0]

        # ── Зоны и уровни сетапа ──────────────────────────────────────────────
        # Рисуются ПОД свечами (zorder ниже), иначе полупрозрачная заливка
        # замыливает тела. Зона входа ярче остальных: на прежних графиках все
        # полосы были одинаково бледными, и нельзя было отличить то, ВО ЧТО
        # входим, от того, что просто рядом.
        for band in geo_bands:
            top = min(float(band['top']), y_max)
            bottom = max(float(band['bottom']), y_min)
            if top <= bottom:
                continue                      # зона целиком за окном
            main = bool(band.get('main'))
            ax.axhspan(bottom, top, facecolor='#2962FF',
                       alpha=0.16 if main else 0.06, zorder=0)
            for edge in (bottom, top):
                ax.axhline(y=edge, color='#2962FF',
                           linewidth=1.1 if main else 0.7,
                           linestyle='-' if main else '--',
                           alpha=0.8 if main else 0.35, zorder=1)
            # ПОДПИСЬ ЗОНЫ СДВИНУТА ОТ ЛЕВОГО КРАЯ, И ЭТО НЕ ВКУСОВЩИНА. У
            # самого края стоят подписи плана — вход, стоп, цель, — а зона
            # часто лежит ровно на цене входа: имбаланс печатался прямо под
            # словом «Entry» и не читался вовсе. Разводим по горизонтали, а не
            # по вертикали: сдвиг вниз увёл бы подпись внутрь чужой зоны.
            ax.text(xlim[0] + x_rng * 0.17, top, band.get('label', ''),
                    color='#7EA6FF', fontsize=8.5,
                    fontweight='bold' if main else 'normal',
                    va='bottom', ha='left', zorder=11,
                    bbox=dict(boxstyle='round,pad=0.15', facecolor='#131722',
                              alpha=0.75, edgecolor='none'))

        # Уровни сетапа подписываются справа. Близкие по цене подписи
        # расталкиваются по вертикали: у SMC пробитый уровень и снятая
        # ликвидность отстоят порой на десятые доли процента, и без разведения
        # две строки печатались одна поверх другой.
        gap = (y_max - y_min) * 0.045
        taken = []
        for guide in sorted(geo_lines, key=lambda g: -float(g['price'])):
            price = float(guide['price'])
            if not (y_min < price < y_max):
                continue
            main = bool(guide.get('main'))
            ax.axhline(y=price, color='#787B86',
                       linewidth=1.2 if main else 0.8,
                       linestyle='-' if main else (0, (1, 4)),
                       alpha=0.85 if main else 0.6, zorder=1)
            at = price
            while any(abs(at - used) < gap for used in taken):
                at -= gap
            taken.append(at)
            ax.text(xlim[1] - x_rng * 0.012, at, guide.get('label', ''),
                    color='#9AA0AC', fontsize=8, va='bottom', ha='right',
                    zorder=11,
                    bbox=dict(boxstyle='round,pad=0.15', facecolor='#131722',
                              alpha=0.75, edgecolor='none'))

        # ── Ровно 3 линии: Entry / SL / TP ────────────────────────────────────
        sl_pct = abs(entry - sl) / entry * 100
        levels = [
            (entry, '#2196F3', '--', f"Entry  ${_fp(entry)}"),
            (sl,    '#F44336', '-',  f"SL  ${_fp(sl)}  (-{sl_pct:.2f}%)"),
            (tp,    '#4CAF50', '-',  f"TP  ${_fp(tp)}  (-{tp_pct:.0f}%)"),
        ]
        for price, color, ls, label in levels:
            ax.axhline(y=price, color=color, linestyle=ls, linewidth=1.5, alpha=0.95, zorder=5)
            ax.text(xlim[0] + x_rng * 0.01, price, label,
                    color=color, fontsize=9, va='bottom', ha='left',
                    fontweight='bold', zorder=12,
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='#131722',
                              alpha=0.7, edgecolor='none'))

        if rr:
            ax.text(0.99, 0.02, f"RR 1:{rr:.1f}", transform=ax.transAxes,
                    color='#B2B5BE', fontsize=8, ha='right', va='bottom')

        tmp_path = os.path.join(tempfile.gettempdir(), f"chart_{pair}_{int(time.time())}.png")
        plt.savefig(tmp_path, dpi=120, bbox_inches='tight', facecolor='#131722', edgecolor='none')
        plt.close(fig)
        return tmp_path

    except Exception as e:
        log(f"chart_generator: ошибка — {e}")
        try:
            import matplotlib.pyplot as plt
            plt.close('all')
        except Exception:
            pass
        return None
