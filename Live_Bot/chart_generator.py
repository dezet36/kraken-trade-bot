"""
Chart generator — 1H candlestick PNG with full SMC/Fibonacci setup:
• Impulse A→B vertical markers
• Fibonacci retracement grid (0%, 38.2%, 61.8%, 78.6%, 88.6%, 100%)
• Zone A / Zone B shading
• Entry, SL (below A for Zone B entry), TP extension targets
• BoS trigger level
"""

import os
import time
import tempfile
import pandas as pd
from logger import log


def generate_trade_chart(signal: dict, df_1h) -> str:
    """
    Generates a 1H candlestick chart PNG with full Fibonacci setup.
    Returns path to a temp PNG file, or None on error.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import mplfinance as mpf
    except ImportError:
        log("chart_generator: matplotlib/mplfinance не установлены — график пропущен")
        return None

    try:
        setup     = signal['setup']
        trigger   = signal['trigger']
        params    = signal['params']
        pair      = signal['trading_pair']
        is_long   = setup['type'] == 'LONG'
        zone_name = trigger.get('zone', '—')
        htf       = signal.get('htf_trend', '—')
        vol_ratio = trigger.get('volume_ratio', 0)
        zone_a    = signal.get('zone_a')
        zone_b    = signal.get('zone_b')

        entry    = float(params['entry'])
        sl       = float(params['stop_loss'])
        tp1      = float(params['take_profit_1'])
        tp2      = float(params['take_profit_2'])
        tp3      = float(params['take_profit_3'])
        tp4      = float(params['take_profit_4'])
        rr       = params.get('rr', 0)

        # A = impulse origin (LOW for LONG, HIGH for SHORT)
        # B = impulse end   (HIGH for LONG, LOW for SHORT)
        a_price  = float(setup['start_price'])
        b_price  = float(setup['end_price'])
        imp_size = float(setup['size'])

        bos_level = trigger.get('bos_level')

        def _fp(p):
            """Adaptive price formatter for any magnitude."""
            if p >= 1000:   return f"{p:.2f}"
            if p >= 1:      return f"{p:.4f}"
            if p >= 0.01:   return f"{p:.6f}"
            if p >= 0.0001: return f"{p:.8f}"
            return f"{p:.10f}"

        # ── Prepare OHLCV ─────────────────────────────────────────────────────
        df = df_1h.copy().reset_index(drop=True)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.set_index('timestamp')
        df = df[['open', 'high', 'low', 'close', 'volume']]
        df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']

        # ── Trim display window: put A ~15 candles from the left edge ─────────
        # Pre-calculate A/B indices before slicing
        try:
            _a_ts = pd.Timestamp(setup['start_time'])
            _b_ts = pd.Timestamp(setup['end_time'])
            _a_idx = int(df.index.get_indexer([_a_ts], method='nearest')[0])
            _b_idx = int(df.index.get_indexer([_b_ts], method='nearest')[0])
            _pre   = 15  # context candles to show before impulse start
            _start = max(0, _a_idx - _pre)
            if _start > 0:
                df = df.iloc[_start:].copy()
        except Exception:
            _start = 0

        # ── Y-axis anchored to impulse + SL + visible TPs ────────────────────
        # Always show the full impulse A→B, SL below A, and at least TP1/TP2
        if is_long:
            y_min = min(sl, a_price - imp_size * 0.06)
            y_max = b_price + imp_size * 0.32   # covers TP1(+18%) and TP2(+27%)
            for tp in [tp3, tp4]:
                if tp <= b_price + imp_size * 1.5:
                    y_max = max(y_max, tp)
            c_lo = float(df['Low'].min())
            c_hi = float(df['High'].max())
            if c_lo >= a_price - imp_size * 0.3:
                y_min = min(y_min, c_lo)
            if c_hi <= b_price + imp_size * 1.5:
                y_max = max(y_max, c_hi)
        else:
            y_max = max(sl, a_price + imp_size * 0.06)
            y_min = b_price - imp_size * 0.32
            for tp in [tp3, tp4]:
                if tp >= b_price - imp_size * 1.5:
                    y_min = min(y_min, tp)
            c_lo = float(df['Low'].min())
            c_hi = float(df['High'].max())
            if c_hi <= a_price + imp_size * 0.3:
                y_max = max(y_max, c_hi)
            if c_lo >= b_price - imp_size * 1.5:
                y_min = min(y_min, c_lo)

        pad   = (y_max - y_min) * 0.03
        y_min -= pad
        y_max += pad

        # ── TradingView dark style ────────────────────────────────────────────
        mc = mpf.make_marketcolors(
            up='#26A69A', down='#EF5350',
            wick={'up': '#26A69A', 'down': '#EF5350'},
            edge={'up': '#26A69A', 'down': '#EF5350'},
            volume={'up': '#26A69A55', 'down': '#EF535055'},
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle=':',
            gridcolor='#2A2E39',
            facecolor='#131722',
            edgecolor='#2A2E39',
            figcolor='#131722',
            rc={
                'axes.labelcolor': '#B2B5BE',
                'xtick.color':     '#B2B5BE',
                'ytick.color':     '#B2B5BE',
                'text.color':      '#D1D4DC',
                'axes.titlecolor': '#FFFFFF',
            },
        )

        dir_icon = "▲ LONG" if is_long else "▼ SHORT"
        vol_str  = f"  Vol {vol_ratio:.1f}×" if vol_ratio > 0 else ""
        htf_str  = f"  HTF: {htf}" if htf not in ('—', 'NEUTRAL', None) else ""
        title    = f"{pair}  {dir_icon}  {zone_name}{htf_str}{vol_str}"

        fig, axes = mpf.plot(
            df,
            type='candle',
            style=style,
            title=title,
            volume=True,
            returnfig=True,
            figsize=(14, 8),
            tight_layout=True,
        )

        ax = axes[0]
        ax.set_ylim(y_min, y_max)
        xlim  = ax.get_xlim()
        y_rng = y_max - y_min
        x_rng = xlim[1] - xlim[0]

        # ── Fibonacci retracement grid (thin dotted, right-side % labels) ─────
        # 0% = A (impulse origin), 100% = B (impulse end)
        # Retracement zones sit BETWEEN A and B
        fib_retracement = [
            (0.000, '#607D8B', '0%  (A)'),
            (0.382, '#00BCD4', '38.2%'),
            (0.618, '#00BCD4', '61.8%'),
            (0.786, '#FF9800', '78.6%'),
            (0.886, '#FF9800', '88.6%'),
            (1.000, '#607D8B', '100% (B)'),
        ]

        used_fib_y = []
        min_fib_gap = y_rng * 0.018

        for ratio, color, label in fib_retracement:
            price = (a_price + ratio * imp_size) if is_long else (a_price - ratio * imp_size)
            if not (y_min <= price <= y_max):
                continue
            ax.axhline(y=price, color=color, linestyle=':', linewidth=0.8,
                       alpha=0.50, zorder=1)
            label_y = price + y_rng * 0.004
            if not any(abs(label_y - uy) < min_fib_gap for uy in used_fib_y):
                ax.text(xlim[1] - x_rng * 0.008, label_y, label,
                        color=color, fontsize=6.5, ha='right', va='bottom',
                        alpha=0.85, zorder=10)
                used_fib_y.append(label_y)

        # ── Key level lines: SL, Entry, TP1-4, BoS ───────────────────────────
        sl_pct  = abs(entry - sl) / entry * 100
        sl_sign = "-" if is_long else "+"
        key_lines = [
            (sl,    '#F44336', '-',  1.8, f'SL  ${_fp(sl)}  ({sl_sign}{sl_pct:.2f}%)'),
            (entry, '#2196F3', '--', 1.8, f'Entry  ${_fp(entry)}'),
            (tp1,   '#4CAF50', '-',  1.3, f'TP1  ${_fp(tp1)}'),
            (tp2,   '#66BB6A', '-',  1.1, f'TP2  ${_fp(tp2)}'),
            (tp3,   '#81C784', '-',  1.0, f'TP3  ${_fp(tp3)}'),
            (tp4,   '#A5D6A7', '-',  1.3, f'TP4  ${_fp(tp4)}'),
        ]
        if bos_level:
            _bl = float(bos_level)
            key_lines.insert(2, (_bl, '#FFD700', ':', 1.0, f'BoS  ${_fp(_bl)}'))

        in_range  = [(p, c, ls, lw, lbl) for p, c, ls, lw, lbl in key_lines
                     if y_min <= p <= y_max]
        above_rng = [(p, c, lbl) for p, c, ls, lw, lbl in key_lines if p > y_max]
        below_rng = [(p, c, lbl) for p, c, ls, lw, lbl in key_lines if p < y_min]

        # Anti-overlap for left-side labels
        used_key_y = []
        min_key_gap = y_rng * 0.022

        for price, color, ls, lw, label in in_range:
            ax.axhline(y=price, color=color, linestyle=ls, linewidth=lw,
                       alpha=0.90, zorder=4)
            label_y = price + y_rng * 0.005
            for _ in range(8):
                if not any(abs(label_y - uy) < min_key_gap for uy in used_key_y):
                    break
                label_y += min_key_gap * 0.7
            used_key_y.append(label_y)
            ax.text(xlim[0] + x_rng * 0.008, label_y, label,
                    color=color, fontsize=8, ha='left', va='bottom',
                    fontweight='bold', zorder=12)

        # Edge arrows for out-of-range key levels
        row_h = y_rng * 0.042
        for i, (price, color, label) in enumerate(above_rng):
            row_y = y_max - row_h * (i + 0.6)
            ax.text(xlim[1] - x_rng * 0.008, row_y, f"  {label} ▲",
                    color=color, fontsize=7.5, ha='right', va='center',
                    fontweight='bold', zorder=12)
        for i, (price, color, label) in enumerate(below_rng):
            row_y = y_min + row_h * (i + 0.6)
            ax.text(xlim[1] - x_rng * 0.008, row_y, f"  {label} ▼",
                    color=color, fontsize=7.5, ha='right', va='center',
                    fontweight='bold', zorder=12)

        # ── Impulse A→B vertical markers ─────────────────────────────────────
        try:
            a_ts  = pd.Timestamp(setup['start_time'])
            b_ts  = pd.Timestamp(setup['end_time'])
            a_idx = int(df.index.get_indexer([a_ts], method='nearest')[0])
            b_idx = int(df.index.get_indexer([b_ts], method='nearest')[0])

            ax.axvline(x=a_idx, color='#26A69A', linestyle='--',
                       linewidth=1.3, alpha=0.75, zorder=2)
            ax.axvline(x=b_idx, color='#EF5350', linestyle='--',
                       linewidth=1.3, alpha=0.75, zorder=2)

            # Badges at the top of the vertical lines
            badge_y = y_max - y_rng * 0.06
            for idx, txt, clr in [(a_idx, 'A', '#26A69A'), (b_idx, 'B', '#EF5350')]:
                x_off = 0.5 if idx < len(df) - 3 else -2.5
                ax.text(idx + x_off, badge_y, txt,
                        color=clr, fontsize=11, fontweight='bold',
                        va='top', zorder=13,
                        bbox=dict(boxstyle='round,pad=0.25', facecolor='#131722',
                                  alpha=0.85, edgecolor=clr, linewidth=0.8))
        except Exception as e:
            log(f"chart_generator: impulse markers — {e}")

        # ── Impulse candle highlight (glow box + arrow on A and B) ──────────
        try:
            import matplotlib.patches as mpatches

            for idx, candle_color, arrow_color in [
                (a_idx, '#26A69A', '#26A69A'),
                (b_idx, '#EF5350', '#EF5350'),
            ]:
                if idx < 0 or idx >= len(df):
                    continue

                c_lo = float(df.iloc[idx]['Low'])
                c_hi = float(df.iloc[idx]['High'])

                # Semi-transparent glow rectangle around the full candle (wick to wick)
                box_lo = max(c_lo - y_rng * 0.005, y_min)
                box_hi = min(c_hi + y_rng * 0.005, y_max)
                rect = mpatches.Rectangle(
                    (idx - 0.55, box_lo),
                    1.10, box_hi - box_lo,
                    linewidth=1.5,
                    edgecolor=candle_color,
                    facecolor=candle_color,
                    alpha=0.18,
                    zorder=3,
                )
                ax.add_patch(rect)

                # Arrow: below candle A (LONG: A is LOW), above candle B (LONG: B is HIGH)
                is_bottom = (is_long and idx == a_idx) or (not is_long and idx == b_idx)
                if is_bottom:
                    arrow_tail_y = max(c_lo - y_rng * 0.045, y_min + y_rng * 0.01)
                    arrow_tip_y  = c_lo
                    if arrow_tail_y < arrow_tip_y and arrow_tail_y >= y_min:
                        ax.annotate('', xy=(idx, arrow_tip_y),
                                    xytext=(idx, arrow_tail_y),
                                    arrowprops=dict(arrowstyle='-|>',
                                                    color=arrow_color, lw=1.8,
                                                    mutation_scale=10),
                                    zorder=14)
                else:
                    arrow_tail_y = min(c_hi + y_rng * 0.045, y_max - y_rng * 0.01)
                    arrow_tip_y  = c_hi
                    if arrow_tail_y > arrow_tip_y and arrow_tail_y <= y_max:
                        ax.annotate('', xy=(idx, arrow_tip_y),
                                    xytext=(idx, arrow_tail_y),
                                    arrowprops=dict(arrowstyle='-|>',
                                                    color=arrow_color, lw=1.8,
                                                    mutation_scale=10),
                                    zorder=14)

        except Exception as e:
            log(f"chart_generator: impulse candle highlight — {e}")

        # ── Zone shading ──────────────────────────────────────────────────────
        def _shade(zone, color, label):
            if zone is None:
                return
            bot = max(float(zone['bottom']), y_min)
            top = min(float(zone['top']),    y_max)
            if top <= bot:
                return
            ax.axhspan(bot, top, alpha=0.22, color=color, zorder=0)
            mid = (bot + top) / 2
            ax.text(xlim[0] + x_rng * 0.38, mid, label,
                    color=color, fontsize=9, va='center', ha='left',
                    alpha=0.90, fontweight='bold', zorder=11)

        _shade(zone_b, '#FF9800', 'Zone B')
        _shade(zone_a, '#00BCD4', 'Zone A')

        # ── Info box (top-left) ───────────────────────────────────────────────
        rr_str   = f"RR 1:{rr:.1f}" if rr else ""
        risk_str = f"${params.get('risk_amount', 0):.2f}"
        info = (
            f"{'📈 LONG' if is_long else '📉 SHORT'}  {zone_name}  {rr_str}\n"
            f"──────────────────\n"
            f"Entry  ${_fp(entry)}\n"
            f"SL     ${_fp(sl)}  ({sl_sign}{sl_pct:.2f}%)\n"
            f"──────────────────\n"
            f"TP1    ${_fp(tp1)}\n"
            f"TP2    ${_fp(tp2)}\n"
            f"TP3    ${_fp(tp3)}\n"
            f"TP4    ${_fp(tp4)}\n"
            f"──────────────────\n"
            f"Risk   {risk_str}"
        )
        ax.text(
            0.012, 0.988, info,
            transform=ax.transAxes,
            color='#D1D4DC', fontsize=7.8, va='top', ha='left',
            fontfamily='monospace',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#0d1117', alpha=0.92,
                      edgecolor='#3d4250', linewidth=0.8),
            zorder=14,
        )

        # ── Save ─────────────────────────────────────────────────────────────
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"chart_{pair}_{int(time.time())}.png"
        )
        plt.savefig(tmp_path, dpi=120, bbox_inches='tight',
                    facecolor='#131722', edgecolor='none')
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
