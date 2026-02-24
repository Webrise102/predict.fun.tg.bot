import os
import sys
import requests
from dotenv import load_dotenv
load_dotenv()

from predict_sdk import (
    OrderBuilder,
    ChainId,
    Order,
    CancelOrdersOptions,
    OrderBuilderOptions,
)

PRIVATE_KEY     = os.getenv("WALLET_PRIVATE_KEY")
API_KEY         = os.getenv("API", "")
PREDICT_ACCOUNT = os.getenv("PREDICT_ACCOUNT", "")
USE_TESTNET     = os.getenv("USE_TESTNET", "false").lower() == "true"

BASE_URL = "https://api.predict.fun"
CHAIN_ID = ChainId.BNB_TESTNET if USE_TESTNET else ChainId.BNB_MAINNET

if not PRIVATE_KEY:
    sys.exit("❌  Установите переменную окружения WALLET_PRIVATE_KEY")


def _base_headers() -> dict:
    h = {}
    if API_KEY:
        h["x-api-key"] = API_KEY
    return h


def authenticate(builder: OrderBuilder) -> str:
    msg_resp = requests.get(f"{BASE_URL}/v1/auth/message", headers=_base_headers(), timeout=15)
    msg_resp.raise_for_status()
    message = msg_resp.json()["data"]["message"]
    print(f"📝  Сообщение для подписи: {message}")

    signature = builder.sign_predict_account_message(message)

    signer_address = PREDICT_ACCOUNT if PREDICT_ACCOUNT else builder._signer.address
    body = {
        "signer":    signer_address,
        "message":   message,
        "signature": signature,
    }

    auth_resp = requests.post(f"{BASE_URL}/v1/auth", json=body, headers=_base_headers(), timeout=15)
    auth_resp.raise_for_status()
    token = auth_resp.json()["data"]["token"]
    print(f"✅  JWT получен для адреса {signer_address}")
    return token


def fetch_all_open_orders(jwt: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {jwt}"}
    if API_KEY:
        headers["x-api-key"] = API_KEY

    resp = requests.get(f"{BASE_URL}/v1/orders", headers=headers, timeout=30)
    resp.raise_for_status()

    result = resp.json()
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(f"Bad API response: {result}")

    data = result.get("data")
    if not isinstance(data, list):
        print(f"   data не список: {data}")
        return []

    orders = []
    for item in data:
        if not isinstance(item, dict):
            continue
        nested = item.get("order") if isinstance(item.get("order"), dict) else {}
        merged = dict(nested)
        merged.update(item)

        status = str(merged.get("status") or merged.get("state") or "").upper()
        if status and status not in {"OPEN", "ACTIVE", "PARTIALLY_FILLED"}:
            continue

        orders.append(item)

    print(f"   Найдено открытых ордеров: {len(orders)}")
    return orders


def main():
    opts    = OrderBuilderOptions(predict_account=PREDICT_ACCOUNT) if PREDICT_ACCOUNT else None
    builder = OrderBuilder.make(CHAIN_ID, PRIVATE_KEY, opts)

    print(f"🔑  Адрес кошелька: {builder._signer.address}")
    print(f"🌐  Сеть: {'Testnet' if USE_TESTNET else 'Mainnet'}")

    jwt = authenticate(builder)

    print("\n📋  Загрузка открытых ордеров...")
    raw_orders = fetch_all_open_orders(jwt)

    if not raw_orders:
        print("✅  Открытых ордеров нет. Ничего отменять не нужно.")
        return

    print(f"📊  Всего открытых ордеров: {len(raw_orders)}")

    regular_orders:                list[Order] = []
    neg_risk_orders:               list[Order] = []
    regular_yield_bearing_orders:  list[Order] = []
    neg_risk_yield_bearing_orders: list[Order] = []

    for item in raw_orders:
        raw_order        = item.get("order", item)
        is_neg_risk      = item.get("isNegRisk", False)
        is_yield_bearing = item.get("isYieldBearing", False)

        if isinstance(raw_order, dict):
            CAMEL_TO_SNAKE = {
                "tokenId":       "token_id",
                "makerAmount":   "maker_amount",
                "takerAmount":   "taker_amount",
                "feeRateBps":    "fee_rate_bps",
                "signatureType": "signature_type",
            }
            mapped = {CAMEL_TO_SNAKE.get(k, k): v for k, v in raw_order.items()}
            ORDER_FIELDS = {"salt", "maker", "signer", "taker", "token_id", "maker_amount",
                            "taker_amount", "expiration", "nonce", "fee_rate_bps", "side",
                            "signature_type"}
            order = Order(**{k: v for k, v in mapped.items() if k in ORDER_FIELDS})
        else:
            order = raw_order
        if is_yield_bearing:
            if is_neg_risk:
                neg_risk_yield_bearing_orders.append(order)
            else:
                regular_yield_bearing_orders.append(order)
        else:
            if is_neg_risk:
                neg_risk_orders.append(order)
            else:
                regular_orders.append(order)

    print(f"   regular:                 {len(regular_orders)}")
    print(f"   negRisk:                 {len(neg_risk_orders)}")
    print(f"   regular + yieldBearing:  {len(regular_yield_bearing_orders)}")
    print(f"   negRisk + yieldBearing:  {len(neg_risk_yield_bearing_orders)}")

    results = {}

    if regular_orders:
        print("\n🚫  Отмена обычных ордеров...")
        results["regular"] = builder.cancel_orders(
            regular_orders,
            options=CancelOrdersOptions(is_neg_risk=False, is_yield_bearing=False),
        )

    if neg_risk_orders:
        print("🚫  Отмена negRisk ордеров...")
        results["neg_risk"] = builder.cancel_orders(
            neg_risk_orders,
            options=CancelOrdersOptions(is_neg_risk=True, is_yield_bearing=False),
        )

    if regular_yield_bearing_orders:
        print("🚫  Отмена regular + yieldBearing ордеров...")
        results["regular_yb"] = builder.cancel_orders(
            regular_yield_bearing_orders,
            options=CancelOrdersOptions(is_neg_risk=False, is_yield_bearing=True),
        )

    if neg_risk_yield_bearing_orders:
        print("🚫  Отмена negRisk + yieldBearing ордеров...")
        results["neg_risk_yb"] = builder.cancel_orders(
            neg_risk_yield_bearing_orders,
            options=CancelOrdersOptions(is_neg_risk=True, is_yield_bearing=True),
        )

    print("\n─── Результат ───────────────────────────────")
    all_success = True
    for name, result in results.items():
        status = "✅" if result.success else "❌"
        print(f"  {status}  {name}: success={result.success}")
        print(f"       details: {vars(result)}")  # ← добавь эту строку
        if not result.success:
            all_success = False

    if all_success:
        print("\n🎉  Все ордера успешно отменены!")
    else:
        print("\n⚠️   Некоторые отмены завершились с ошибкой.")


if __name__ == "__main__":
    main()