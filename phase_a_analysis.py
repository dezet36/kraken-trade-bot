"""
phase_a_analysis.py — бесплатная разведка по готовому детальному CSV (D1B @ 0.5%/cap3).
Ищем асимметрии, которые укажут, какие эксперименты обещают наибольший прирост:
направление, час суток, день недели, длительность сделки, эффект безубытка,
геометрия SL-дистанции. Ничего не симулирует — только pandas по 670 сделкам.
"""
import sys
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else r'D:\Bot trade\backtest_campaign_D1B_r05_c3.csv'
df = pd.read_csv(path)
df['entry_time'] = pd.to_datetime(df['entry_time'])
df['exit_time'] = pd.to_datetime(df['exit_time'])
df['dur_h'] = (df['exit_time'] - df['entry_time']).dt.total_seconds() / 3600
df['win'] = df['R'] > 0
df['sl_dist_pct'] = (df['entry'] - df['sl']).abs() / df['entry'] * 100


def block(g, label):
    n = len(g)
    if n == 0:
        print(f'{label:<22} —')
        return
    wr = g['win'].mean() * 100
    sumr = g['R'].sum()
    avgr = g['R'].mean()
    gp = g.loc[g['R'] > 0, 'R'].sum()
    gl = -g.loc[g['R'] < 0, 'R'].sum()
    pf = gp / gl if gl > 0 else float('inf')
    print(f'{label:<22} n={n:<5} WR={wr:5.1f}%  sumR={sumr:+8.2f}  avgR={avgr:+.3f}  PF={pf:.2f}')


print(f'Файл: {path} | сделок: {len(df)}')
print('\n===== ПО НАПРАВЛЕНИЮ =====')
for d, g in df.groupby('dir'):
    block(g, d)

print('\n===== ПО НАПРАВЛЕНИЮ × ПАРА (только выразительные) =====')
pv = df.groupby(['pair', 'dir'])['R'].agg(['count', 'sum']).round(1)
print(pv.to_string())

print('\n===== ПО ЧАСУ ВХОДА (UTC) =====')
df['hour'] = df['entry_time'].dt.hour
for h, g in df.groupby('hour'):
    block(g, f'  {h:02d}:00')

print('\n===== ПО 6-ЧАСОВЫМ СЕССИЯМ (UTC) =====')
df['sess'] = pd.cut(df['hour'], bins=[-1, 5, 11, 17, 23],
                    labels=['00-06 (Азия ночь)', '06-12 (ЕУ утро)', '12-18 (США)', '18-24 (вечер)'])
for s, g in df.groupby('sess', observed=True):
    block(g, str(s))

print('\n===== ПО ДНЮ НЕДЕЛИ =====')
df['dow'] = df['entry_time'].dt.day_name()
order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
for d in order:
    block(df[df['dow'] == d], d)

print('\n===== ПО ДЛИТЕЛЬНОСТИ СДЕЛКИ =====')
df['dur_b'] = pd.cut(df['dur_h'], bins=[0, 4, 12, 24, 48, 1e9],
                     labels=['<4ч', '4-12ч', '12-24ч', '24-48ч', '>48ч'])
for b, g in df.groupby('dur_b', observed=True):
    block(g, str(b))

print('\n===== ЭФФЕКТ БЕЗУБЫТКА (be=True: BE был взведён) =====')
for b, g in df.groupby('be'):
    block(g, f'be={b}')
print('  exit_reason counts:')
print(df.groupby(['be', 'exit_reason']).size().to_string())

print('\n===== ПО SL-ДИСТАНЦИИ (% от entry; прокси размера импульса) =====')
df['slb'] = pd.cut(df['sl_dist_pct'], bins=[0, 1.0, 1.5, 2.0, 3.0, 100],
                   labels=['<1%', '1-1.5%', '1.5-2%', '2-3%', '>3%'])
for b, g in df.groupby('slb', observed=True):
    block(g, str(b))

print('\n===== ПО RR СДЕЛКИ =====')
df['rrb'] = pd.cut(df['rr'], bins=[0, 2.29, 2.5, 3.0, 4.0, 1000],
                   labels=['~2.28 (геометрия)', '2.3-2.5', '2.5-3', '3-4', '>4 (пол SL)'])
for b, g in df.groupby('rrb', observed=True):
    block(g, str(b))

print('\n===== ПОЛОВИНЫ ПЕРИОДА (стабильность во времени) =====')
mid = df['exit_time'].min() + (df['exit_time'].max() - df['exit_time'].min()) / 2
block(df[df['exit_time'] < mid], f'1-я половина (до {mid.date()})')
block(df[df['exit_time'] >= mid], '2-я половина')

print('\n===== ПОМЕСЯЧНО =====')
df['month'] = df['exit_time'].dt.to_period('M')
for m, g in df.groupby('month'):
    block(g, str(m))
