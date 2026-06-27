import os
from datetime import datetime

LOG_FILE = "bot_log.txt"

def log(message, level="INFO"):
    """Записывает сообщение в лог и выводит в консоль"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{level}] {message}"
    
    print(entry)
    
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

def log_trade(trade_data):
    """Записывает сделку в CSV файл"""
    csv_file = "trades.csv"

    write_header = not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0

    with open(csv_file, "a", encoding="utf-8") as f:
        if write_header:
            f.write("time,pair,direction,entry,exit,pnl,zone,result,mode\n")

        f.write(f"{trade_data['time']},{trade_data['pair']},{trade_data['direction']},{trade_data['entry']},{trade_data['exit']},{trade_data['pnl']},{trade_data['zone']},{trade_data['result']},{trade_data.get('mode', 'DEMO')}\n")