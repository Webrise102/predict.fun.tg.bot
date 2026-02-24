import re
from datetime import datetime, timedelta

# ─── Парсинг файла 1 (Funding) ─────────────────────────────────────────────
def calc_funding(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    quantities = re.findall(r'USDC\s+([-\d.]+)\s+Funding', content)
    return sum(float(q) for q in quantities)

# ─── Парсинг файла 2 (Closed P&L) → список (токен, время закрытия, pnl) ───
def parse_file2(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    trades = []
    blocks = content.split('Details')

    for block in blocks:
        block = block.strip()
        if not block or block.startswith('Market'):
            continue

        # Токен из названия рынка (FARTCOIN-USD → FARTCOIN)
        market_match = re.search(r'^(\w+)-USD', block, re.MULTILINE)
        if not market_match:
            continue
        token = market_match.group(1)

        # P&L значение
        pnl_match = re.search(r'(-?\$[\d,]+\.?\d*)\s*\n\s*-?[\d.]+%', block)
        if not pnl_match:
            continue
        pnl = float(pnl_match.group(1).replace('$', '').replace(',', ''))

        # Время закрытия — второй datetime в блоке
        times = re.findall(r'(\d{4}-\d{2}-\d{2})\s*\n\s*(\d{2}:\d{2}:\d{2})', block)
        if len(times) < 2:
            continue
        close_dt = datetime.strptime(f"{times[1][0]} {times[1][1]}", "%Y-%m-%d %H:%M:%S")

        trades.append((token, close_dt, pnl))

    return trades

# ─── Парсинг файла 3 (Realized PNL) → список (токен, время, pnl) ──────────
def parse_file3(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    trades = []
    pattern = r'USDC\s+([-\d.]+)\s+Realized PNL\s+Confirmed\s+(\w+)-PERP\s+(\d{4}-\d{2}-\d{2})\s*\n\s*(\d{2}:\d{2}:\d{2})'
    matches = re.findall(pattern, content)

    for qty, token, date, time in matches:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
        trades.append((token, dt, float(qty)))

    return trades

# ─── Фильтрация: оставить только пары с совпадением ±1 час по токену ───────
def filter_matched(trades2, trades3, window_hours=1):
    matched2, matched3 = [], []
    used3 = set()
    totalLoss = 0

    for token2, dt2, pnl2 in trades2:
        for j, (token3, dt3, pnl3) in enumerate(trades3):
            if j in used3:
                continue
            if token2 == token3 and abs((dt2 - dt3).total_seconds()) <= window_hours * 3600:
                matched2.append((token2, dt2, pnl2))
                matched3.append((token3, dt3, pnl3))
                used3.add(j)
                print(f"  ✓ Совпадение: {token2} | file2: {dt2} | file3: {dt3}")
                break
        else:
            print(f"  ✗ Без пары (удалено): {token2} | {dt2} | P&L: ${pnl2:.4f}")
            totalLoss+=pnl2
    print(f"Total loss without delta-neutral: {totalLoss}")
    return matched2, matched3

# ─── Итог ──────────────────────────────────────────────────────────────────
funding = calc_funding(r"C:\Users\ilahr\OneDrive\Рабочий стол\variationalFunding.txt")

trades2 = parse_file2(r"C:\Users\ilahr\OneDrive\Рабочий стол\etherealExport.txt")
trades3 = parse_file3(r"C:\Users\ilahr\OneDrive\Рабочий стол\variationalExport.txt")

print("\nМатчинг сделок:")
matched2, matched3 = filter_matched(trades2, trades3)

pnl_closed   = sum(pnl for _, _, pnl in matched2)
pnl_realized = sum(pnl for _, _, pnl in matched3)
grand_total  = funding + pnl_closed + pnl_realized

print(f"\nFunding (file1):        ${funding:.4f}")
print(f"Closed P&L (file2):     ${pnl_closed:.4f}  ({len(matched2)} сделок)")
print(f"Realized PNL (file3):   ${pnl_realized:.4f}  ({len(matched3)} сделок)")
print(f"─────────────────────────────────")
print(f"Итого:                  ${grand_total:.4f}")