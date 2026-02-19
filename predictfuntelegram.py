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
import asyncio
import aiohttp
import jwt as pyjwt
from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions

load_dotenv()

ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
ORDERS_URL = "https://api.predict.fun/v1/orders"
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
RENDER_PORT = int(os.getenv("PORT", "10000"))

# ДОБАВИТЬ вместо них:
_PRIVATE_KEY    = os.getenv("WALLET_PRIVATE_KEY", "")
PREDICT_API_KEY = os.getenv("API", "")
PREDICT_ACCOUNT = os.getenv("PREDICT_ACCOUNT", "")
PREDICT_BASE_URL = os.getenv("PREDICT_BASE_URL", "https://api.predict.fun")

class JWTManager:
    REFRESH_BEFORE_EXPIRY = 5 * 60

    def __init__(self, private_key, api_key, predict_account=""):
        self._private_key     = private_key
        self._api_key         = api_key
        self._predict_account = predict_account
        self._token           = ""
        self._lock            = asyncio.Lock()

    async def get_headers(self) -> dict:
        await self._ensure_valid()
        h = {"Authorization": f"Bearer {self._token}"}
        if self._api_key:
            h["x-api-key"] = self._api_key
        return h

    async def force_refresh(self):
        async with self._lock:
            self._token = await asyncio.to_thread(self._fetch_jwt)

    async def initialize(self):
        await self.force_refresh()

    async def _ensure_valid(self):
        if not self._token or self._is_expiring_soon():
            async with self._lock:
                if not self._token or self._is_expiring_soon():
                    self._token = await asyncio.to_thread(self._fetch_jwt)

    def _is_expiring_soon(self) -> bool:
        try:
            payload   = pyjwt.decode(self._token, options={"verify_signature": False}, algorithms=["HS256", "RS256"])
            remaining = payload.get("exp", 0) - time.time()
            return remaining < self.REFRESH_BEFORE_EXPIRY
        except Exception:
            return True

    # ЗАМЕНИТЬ НА:
    def _fetch_jwt(self) -> str:
        from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions

        base_headers = {"x-api-key": self._api_key} if self._api_key else {}

        builder = OrderBuilder.make(
            ChainId.BNB_MAINNET,
            self._private_key,
            OrderBuilderOptions(predict_account=self._predict_account) if self._predict_account else None,
        )

        msg = requests.get(f"{PREDICT_BASE_URL}/v1/auth/message", headers=base_headers, timeout=15)
        msg.raise_for_status()
        message = msg.json()["data"]["message"]

        signature = builder.sign_predict_account_message(message)

        body = {
            "signer":    self._predict_account if self._predict_account else builder.signer.address,
            "message":   message,
            "signature": signature,
        }

        resp = requests.post(f"{PREDICT_BASE_URL}/v1/auth", json=body, headers=base_headers, timeout=15)
        resp.raise_for_status()
        token = resp.json()["data"]["token"]
        return token
jwt_manager = JWTManager(_PRIVATE_KEY, PREDICT_API_KEY, PREDICT_ACCOUNT)


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


# ЗАМЕНИТЬ:
async def fetch_open_limit_orders() -> list[dict]:
    headers  = await jwt_manager.get_headers()
    response = requests.get(ORDERS_URL, headers=headers, timeout=30)
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
    if update.effective_user.id != ALLOWED_USER_ID:
        return  # молча игнорируем чужие запросы
    try:
        await update.message.reply_text("Loading limit orders...")
        orders = await fetch_open_limit_orders()
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
    
    headers = asyncio.get_event_loop().run_until_complete(jwt_manager.get_headers())
    orders_response = requests.get(f"{PREDICT_BASE_URL}/v1/orders", headers=headers).json()
    orders_r = orders_response["data"]
    
    for o in orders_r:
        market_id = o["marketId"]
        r = requests.get(f"{PREDICT_BASE_URL}/v1/markets/{market_id}/orderbook", headers=headers)
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

# ДОБАВИТЬ:
async def fetch(session, url):
    headers = await jwt_manager.get_headers()
    async with session.get(url, headers=headers) as response:
        if response.status == 401:
            await jwt_manager.force_refresh()
            headers = await jwt_manager.get_headers()
            async with session.get(url, headers=headers) as r2:
                return await r2.json()
        return await response.json()
async def bids_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return  # молча игнорируем чужие запросы
    notifications = []
    async with aiohttp.ClientSession() as session:
        orders_data = await fetch(session, "https://api.predict.fun/v1/orders")
        orders = orders_data.get("data", [])
        if not orders:
            await update.message.reply_text("Ордеров не найдено.")
            return
        market_ids = list(set(o["marketId"] for o in orders))
        orderbook_tasks = [fetch(session, f"https://api.predict.fun/v1/markets/{m_id}/orderbook") for m_id in market_ids]
        market_info_tasks = [fetch(session, f"https://api.predict.fun/v1/markets/{m_id}") for m_id in market_ids]
        results = await asyncio.gather(*orderbook_tasks, *market_info_tasks)
        n = len(market_ids)
        orderbooks = {market_ids[i]: results[i]["data"] for i in range(n)}
        titles = {market_ids[i]: results[i+n]["data"] for i in range(n)}
        for o in orders:
            m_id = o["marketId"]
            orderbook_data = orderbooks[m_id]
            title_data = titles[m_id]
            question = title_data["question"]
            analyze = analyze_order(o, orderbook_data)
            maker_amt = float(o["order"]["makerAmount"])
            taker_amt = float(o["order"]["takerAmount"])
            my_price = maker_amt / taker_amt
            my_shares = taker_amt / 1e18
            my_usd = my_price * my_shares
            if analyze["likely_outcome"] == "YES":
                bids = orderbook_data.get("bids", [])
            else:
                no_book = transform_to_no_orderbook(orderbook_data, precision=3)
                bids = no_book.get("no_bids", [])
            higher = [b for b in bids if b[0] > my_price]
            lower = [b for b in bids if b[0] <= my_price][:3]
            quote_lines = []
            for price, shares in higher:
                quote_lines.append(f"{price*100:>6.2f}¢ | {shares:>8.2f} sh | ${price * shares:>8.2f}")
            
            quote_lines.append(f"<b>▶ {my_price*100:>6.2f}¢ | {my_shares:>8.2f} sh | ${my_usd:>8.2f} ← YOUR ORDER</b>")
            
            for price, shares in lower:
                quote_lines.append(f"{price*100:>6.2f}¢ | {shares:>8.2f} sh | ${price * shares:>8.2f}")
            
            quote_text = "\n".join(quote_lines)
            notifications.append(
                f"<code>{question}</code>\n"
                f"<blockquote>{quote_text}</blockquote>\n\n"
            )
    full_message = "".join(notifications)
    if len(full_message) > 4000:
        for i in range(0, len(full_message), 4000):
            await update.message.reply_text(full_message[i:i+4000], parse_mode="HTML")
    else:
        await update.message.reply_text(full_message, parse_mode="HTML")







def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN (or TELEGRAM_TOKEN) is not set.")

    start_keepalive_server()
    delete_webhook_if_needed()
    # ДОБАВИТЬ перед app = ApplicationBuilder()...:
    asyncio.run(jwt_manager.initialize())
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("bids", bids_command))

    print("Bot started")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
