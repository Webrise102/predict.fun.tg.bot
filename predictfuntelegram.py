import asyncio
import html
import os
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

ORDERS_URL = "https://api.predict.fun/v1/orders"
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
RENDER_PORT = int(os.getenv("PORT", "10000"))

HEADERS = {
    "Authorization": f"Bearer {os.getenv('JWT', '')}",
    "x-api-key": os.getenv("API", ""),
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        return


def start_keepalive_server() -> None:
    def run_server():
        server = ThreadingHTTPServer(("0.0.0.0", RENDER_PORT), HealthHandler)
        print(f"HTTP server started on 0.0.0.0:{RENDER_PORT}")
        server.serve_forever()

    Thread(target=run_server, daemon=True).start()


def delete_webhook_if_needed() -> None:
    if not BOT_TOKEN:
        return
    try:
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": "true"},
            timeout=20,
        )
    except requests.RequestException:
        pass


def _as_decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_token_units(value) -> Decimal | None:
    number = _as_decimal(value)
    if number is None:
        return None
    if abs(number) >= Decimal("1e12"):
        return number / Decimal("1e18")
    return number


def _extract_price(order: dict) -> Decimal | None:
    price_keys = ("price", "limitPrice", "orderPrice", "targetPrice", "rawPrice")
    for key in price_keys:
        price = _to_token_units(order.get(key))
        if price is not None:
            return price

    maker_amount = _to_token_units(order.get("makerAmount"))
    taker_amount = _to_token_units(order.get("takerAmount"))
    if not maker_amount or not taker_amount:
        return None

    side = str(order.get("side", "")).upper()
    ratio_maker_taker = maker_amount / taker_amount
    ratio_taker_maker = taker_amount / maker_amount

    if side in {"BUY", "BID", "0"}:
        return ratio_maker_taker
    if side in {"SELL", "ASK", "1"}:
        return ratio_taker_maker

    if Decimal("0") < ratio_maker_taker <= Decimal("1"):
        return ratio_maker_taker
    if Decimal("0") < ratio_taker_maker <= Decimal("1"):
        return ratio_taker_maker
    return ratio_maker_taker


def _to_side_text(value) -> str:
    if value in ("BUY", "buy", "BID", "bid", 0, "0"):
        return "BUY"
    if value in ("SELL", "sell", "ASK", "ask", 1, "1"):
        return "SELL"
    return "N/A"


def _normalize_order(item: dict) -> dict:
    nested = item.get("order") if isinstance(item.get("order"), dict) else {}
    merged = dict(nested)
    merged.update(item)
    return merged


def fetch_open_limit_orders() -> list[dict]:
    response = requests.get(ORDERS_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    result = response.json()
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(f"Bad API response: {result}")

    data = result.get("data")
    if not isinstance(data, list):
        return []

    orders = []
    for item in data:
        if not isinstance(item, dict):
            continue
        merged = _normalize_order(item)

        strategy = str(merged.get("strategy") or merged.get("type") or "").upper()
        status = str(merged.get("status") or merged.get("state") or "").upper()

        if strategy and "LIMIT" not in strategy:
            continue
        if status and status not in {"OPEN", "ACTIVE", "PARTIALLY_FILLED"}:
            continue

        orders.append(merged)

    return orders


def format_orders_message(orders: list[dict]) -> str:
    if not orders:
        return "No active limit orders."

    lines = [f"Active limit orders: {len(orders)}"]

    for idx, order in enumerate(orders[:25], start=1):
        market_id = order.get("marketId") or "-"
        order_id = order.get("id") or order.get("hash") or "n/a"
        side = _to_side_text(order.get("side"))
        status = str(order.get("status") or "N/A").upper()

        price = _extract_price(order)
        amount = _to_token_units(order.get("amount"))
        if amount is None:
            amount = _to_token_units(order.get("remainingAmount"))

        price_text = f"{(price * 100):.2f}¢" if price is not None else "n/a"
        amount_text = f"{amount:.2f}" if amount is not None else "n/a"
        value_text = f"${(amount * price):.2f}" if amount is not None and price is not None else "n/a"

        lines.append(
            f"{idx}. <b>market #{html.escape(str(market_id))}</b>\n"
            f"{side} | {status}\n"
            f"Shares: {amount_text} | Price: {price_text} | Value: {value_text}\n"
            f"ID: <code>{html.escape(str(order_id))}</code>"
        )

    if len(orders) > 25:
        lines.append(f"... and {len(orders) - 25} more")

    return "\n\n".join(lines)


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("Loading limit orders...")
        orders = fetch_open_limit_orders()
        await update.message.reply_text(format_orders_message(orders), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /orders to view active limit orders.")

def get_complement(price, decimal_precision=2):
    factor = 10 ** decimal_precision
    return (factor - round(price * factor)) / factor

def transform_to_no_orderbook(yes_orderbook, precision=2):
    yes_asks = yes_orderbook.get("asks", [])
    yes_bids = yes_orderbook.get("bids", [])
    no_asks = [[get_complement(p, precision), q] for p, q in yes_bids]
    no_bids = [[get_complement(p, precision), q] for p, q in yes_asks]
    return {
        "marketId": yes_orderbook.get("marketId"),
        "no_asks": no_asks,
        "no_bids": no_bids
    }
def analyze_order(order_data, orderbook):
    m_amt = int(order_data['order']['makerAmount']) / 10**18
    t_amt = int(order_data['order']['takerAmount']) / 10**18
    order_price = round(m_amt / t_amt, 3)
    token_id = order_data['order']['tokenId']
    best_yes_bid = orderbook['bids'][0][0] if orderbook['bids'] else 0
    best_yes_ask = orderbook['asks'][0][0] if orderbook['asks'] else 0
    diff_yes = abs(order_price - best_yes_bid)
    diff_no = abs((1 - order_price) - best_yes_bid)
    
    side = "YES" if diff_yes < diff_no else "NO"
    
    return {
        "calculated_price": order_price,
        "likely_outcome": side,
        "token_id": token_id
    }
prev_highest_bids = {}

def aggregate_notifications():
    global prev_highest_bids
    notifications = []
    
    orders_response = requests.request("GET", "https://api.predict.fun/v1/orders", headers=HEADERS).json()
    orders_r = orders_response["data"]
    
    for o in orders_r:
        market_id = o["marketId"]
        r = requests.get(f"https://api.predict.fun/v1/markets/{market_id}/orderbook", headers=HEADERS)
        orderbook_data = r.json()["data"]
        analyze = analyze_order(o, orderbook_data)
        if analyze["likely_outcome"] == 'YES':
            highest_bid = orderbook_data["bids"][0][0]
        else:
            no_book = transform_to_no_orderbook(orderbook_data, precision=3)
            highest_bid = no_book['no_bids'][0][0] 
        if market_id not in prev_highest_bids:
            prev_highest_bids[market_id] = highest_bid
            continue
        last_price = prev_highest_bids[market_id]
        if last_price > highest_bid:
            print(f"[{market_id}] bid price comes closer (dropped)")
            notifications.append(f"Bid price close: {market_id}; was: {last_price}, now: {highest_bid}")
            
        elif last_price < highest_bid:
            print(f"[{market_id}] bid price became bigger")
            notifications.append(f"Bid price bigger: {market_id}; was: {last_price}, now: {highest_bid}")
        prev_highest_bids[market_id] = highest_bid
        
    return notifications


async def bids_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        notifications = []
        orders_response = requests.request("GET", "https://api.predict.fun/v1/orders", headers=HEADERS).json()
        orders_r = orders_response["data"]
        for o in orders_r:
            market_id = o["marketId"]
            r = requests.get(f"https://api.predict.fun/v1/markets/{market_id}/orderbook", headers=HEADERS)
            orderbook_data = r.json()["data"]
            titleRequest = requests.get(f"https://api.predict.fun/v1/markets/{market_id}", headers=HEADERS)
            analyze = analyze_order(o, orderbook_data)
            if analyze["likely_outcome"] == 'YES':
                highest_bid = orderbook_data["bids"][0][0]
            else:
                no_book = transform_to_no_orderbook(orderbook_data, precision=3)
                highest_bid = no_book['no_bids'][0][0] 
            notifications.append(f"<code>{titleRequest.json()["data"]["question"]}</code> \n<b>Value: ${float(o["order"]["makerAmount"])/1000000000000000000} | Order: {float(o["order"]["makerAmount"])/float(o["order"]["takerAmount"]) * 100}¢ | Bid: {highest_bid * 100}¢</b>\n\n")
        await update.message.reply_text("".join(notifications), parse_mode="HTML")





def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN (or TELEGRAM_TOKEN) is not set.")

    start_keepalive_server()
    delete_webhook_if_needed()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("bids", bids_command))

    print("Bot started")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
