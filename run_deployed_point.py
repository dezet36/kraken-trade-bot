"""
Точка risk=0.5%/cap=3 для D1, D1B (точный live-конфиг), D3 — на одной и той же
точке параметров, чтобы сравнение было честным (существующий backtest_campaign.csv
посчитан на risk=1%/cap=5, а не на задеплоенных 0.5%/3).

Не трогает Live_Bot/. Данные — из backtest_cache/, сети не будет (кэш уже прогрет).
"""
import sys
import time

sys.path.insert(0, r'D:\Bot trade')
import backtest as bt
import backtest_campaign as camp

t0 = time.time()
data_1h, data_5m = bt.load_all_data()

RISK, CAP = 0.5, 3
configs = [
    ({'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': False}, 'D1',
     r'D:\Bot trade\backtest_campaign_D1_r05_c3.csv'),
    ({'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True},  'D1B',
     r'D:\Bot trade\backtest_campaign_D1B_r05_c3.csv'),
    ({'e1b': False, 'e2': False, 'bos': False, 'ntp': 2, 'be': True},  'D3',
     r'D:\Bot trade\backtest_campaign_D3_r05_c3.csv'),
]

for cfg, name, path in configs:
    camp.run_config(data_1h, data_5m, cfg, f'{name} @ risk={RISK}%/cap={CAP}',
                     save_csv=True, risk_pct=RISK, cap=CAP, csv_path=path)

print(f'\nВремя: {(time.time()-t0)/60:.1f} мин')
