"""
Chart generator — чистый часовой график в стиле TradingView:
• свечи 1H (тёмная тема)
• ровно 3 уровня сделки: Entry / Stop Loss / Take Profit (подписаны)
• нижняя панель RSI(14) с уровнями 70/30
Без сетки Фибоначчи, зон, меток A/B и инфо-бокса — лаконичный вид как в TradingView.
"""

import os
import time
import tempfile
import pandas as pd
from logger import log


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-9)
    return (100 - 100 / (1 + rs)).fillna(50)


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
        tp    = float(params['take_profit_1'])     # один тейк -18%
        rr    = params.get('rr', 0)

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

        # ── Y-границы: свечи + уровни ─────────────────────────────────────────
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

        # ── Ровно 3 линии: Entry / SL / TP ────────────────────────────────────
        sl_pct = abs(entry - sl) / entry * 100
        levels = [
            (entry, '#2196F3', '--', f"Entry  ${_fp(entry)}"),
            (sl,    '#F44336', '-',  f"SL  ${_fp(sl)}  (-{sl_pct:.2f}%)"),
            (tp,    '#4CAF50', '-',  f"TP  ${_fp(tp)}  (-18%)"),
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
