"""
pair_breakdown.py — sumR/WR/PF/concentration по парам из детального CSV кампании.
Использование: python pair_breakdown.py [путь_к_csv]  (по умолчанию backtest_campaign.csv)
"""
import sys
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else r'D:\Bot trade\research\results\backtest_campaign.csv'
df = pd.read_csv(path)


def _pf(g):
    gp = g.loc[g['R'] > 0, 'R'].sum()
    gl = -g.loc[g['R'] < 0, 'R'].sum()
    return gp / gl if gl > 0 else float('inf')


grp = df.groupby('pair')
summary = pd.DataFrame({
    'n_trades': grp.size(),
    'wins':     grp.apply(lambda g: int((g['R'] > 0).sum())),
    'wr_pct':   grp.apply(lambda g: (g['R'] > 0).mean() * 100).round(1),
    'sumR':     grp['R'].sum().round(2),
    'avgR':     grp['R'].mean().round(3),
    'pf':       grp.apply(_pf).round(2),
})
if 'pnl' in df.columns:
    summary['sum_pnl_usd'] = grp['pnl'].sum().round(2)

total_posR = df.loc[df['R'] > 0, 'R'].sum()
summary['pct_of_total_posR'] = (
    grp.apply(lambda g: g.loc[g['R'] > 0, 'R'].sum()) / total_posR * 100
    if total_posR > 0 else 0.0
).round(1)
summary = summary.sort_values('sumR', ascending=False)

print(f'\nФайл: {path}')
print(f'Всего сделок: {len(df)} | пар: {df["pair"].nunique()} | sumR: {df["R"].sum():+.1f}\n')
print(summary.to_string())

if len(summary):
    top_pair, top_pct = summary['pct_of_total_posR'].idxmax(), summary['pct_of_total_posR'].max()
    print(f'\nТоп-пара по вкладу в общий положительный R: {top_pair} = {top_pct:.1f}%')
    if top_pct > 30:
        print('  >30% — результат сильно зависит от одной пары (высокая концентрация).')
    elif top_pct > 20:
        print('  20-30% — заметная концентрация, держите в уме.')
    else:
        print('  Концентрация в пределах разумного (<20%).')
