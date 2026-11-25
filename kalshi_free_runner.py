"""One scheduled check for a small Kalshi liquidity-incentive order.

Dry-run is the default. Real order writes require KALSHI_LIVE=true in the
GitHub repository's Actions variables. Credentials are loaded only from
environment variables and are never written to disk or logs.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
BASE = "https://external-api.kalshi.com"
API_PREFIX = "/trade-api/v2"
ORDER_TAG = "KLLK-"


class StopRun(Exception):
    pass


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} {message}", flush=True)


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return fallback.copy()
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state: dict) -> None:
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("stop_at_utc must include Z or a UTC offset")
    return parsed.astimezone(timezone.utc)


def decimal_value(value) -> Decimal:
    return Decimal(str(value or "0"))


class Kalshi:
    def __init__(self) -> None:
        self.key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
        key_text = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "").strip()
        if not self.key_id or not key_text:
            raise RuntimeError("Add KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM as GitHub Actions secrets.")
        self.private_key = serialization.load_pem_private_key(key_text.encode(), password=None)

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        query = f"?{urlencode(params)}" if params else ""
        full_path = API_PREFIX + path
        timestamp = str(int(time.time() * 1000))
        presign = (timestamp + method.upper() + full_path).encode()
        signature = self.private_key.sign(
            presign,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        response = requests.request(
            method.upper(), BASE + full_path + query, headers=headers, json=body, timeout=20
        )
        if not response.ok:
            raise requests.HTTPError(f"Kalshi returned HTTP {response.status_code}: {response.text[:250]}", response=response)
        return response.json() if response.text else {}

    def market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}").get("market", {})

    def orderbook(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}/orderbook").get("orderbook_fp", {})

    def positions(self, ticker: str) -> list[dict]:
        response = self.request("GET", "/portfolio/positions", {"ticker": ticker, "count_filter": "position"})
        return response.get("market_positions", [])

    def orders(self, ticker: str) -> list[dict]:
        response = self.request("GET", "/portfolio/orders", {"ticker": ticker, "status": "resting", "limit": 1000})
        return response.get("orders", [])

    def balance_dollars(self) -> Decimal:
        response = self.request("GET", "/portfolio/balance")
        if "balance_dollars" in response:
            return decimal_value(response["balance_dollars"])
        return decimal_value(response.get("balance", 0)) / Decimal(100)

    def cancel(self, order_id: str, ticker: str) -> None:
        self.request("DELETE", f"/portfolio/events/orders/{order_id}", {"market_ticker": ticker})

    def create_yes_bid(self, ticker: str, count: Decimal, price_cents: int, expires_ts: int) -> dict:
        body = {
            "ticker": ticker,
            "client_order_id": ORDER_TAG + uuid.uuid4().hex,
            "side": "bid",
            "count": f"{count:.2f}",
            "price": f"{Decimal(price_cents) / Decimal(100):.4f}",
            "time_in_force": "good_till_canceled",
            "expiration_time": expires_ts,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": True,
            "cancel_order_on_pause": True,
            "exchange_index": -1,
        }
        return self.request("POST", "/portfolio/events/orders", body=body)


def best_price_cents(levels) -> int | None:
    if not levels:
        return None
    price_dollars = max(decimal_value(level[0]) for level in levels)
    return int((price_dollars * 100).to_integral_value(rounding=ROUND_FLOOR))


def owned_orders(kalshi: Kalshi, ticker: str) -> tuple[list[dict], list[dict]]:
    orders = kalshi.orders(ticker)
    ours = [o for o in orders if str(o.get("client_order_id", "")).startswith(ORDER_TAG)]
    other_yes_bids = [
        o for o in orders
        if not str(o.get("client_order_id", "")).startswith(ORDER_TAG)
        and o.get("book_side") == "bid"
        and o.get("outcome_side") == "yes"
    ]
    return ours, other_yes_bids


def cancel_ours(kalshi: Kalshi, ticker: str) -> None:
    for order in kalshi.orders(ticker):
        if str(order.get("client_order_id", "")).startswith(ORDER_TAG):
            log(f"Requesting cancellation of tagged resting order {order.get('order_id')}.")
            kalshi.cancel(order["order_id"], ticker)


def halt(kalshi: Kalshi, config: dict, state: dict, reason: str) -> None:
    state["halted"] = True
    state["halt_reason"] = reason
    log(f"HALT: {reason}")
    try:
        cancel_ours(kalshi, config["ticker"])
    except Exception as exc:
        log(f"Cancellation attempt failed: {exc}. Check Kalshi → Portfolio → Orders.")


def run_once(config: dict, state: dict, kalshi: Kalshi, live: bool) -> None:
    ticker = config["ticker"].strip()
    if not ticker or ticker.startswith("REPLACE_"):
        raise StopRun("Set config.json to an exact active rewarded market ticker before running.")
    if config.get("side", "yes").lower() != "yes":
        raise StopRun("This template is intentionally limited to YES-side bids.")

    stop_time = iso_utc(config["stop_at_utc"])
    now = datetime.now(timezone.utc)
    utc_day = now.date().isoformat()
    if state.get("utc_day") != utc_day:
        state["utc_day"] = utc_day
        state["moves_today"] = 0
    if state.get("heartbeat_day") != utc_day:
        state["heartbeat_day"] = utc_day

    if state.get("halted"):
        log(f"Already halted: {state.get('halt_reason') or 'manual stop'}.")
        cancel_ours(kalshi, ticker)
        return
    if now >= stop_time:
        halt(kalshi, config, state, "The configured incentive end time has passed.")
        return

    market = kalshi.market(ticker)
    if market.get("status") not in ("active", "open"):
        halt(kalshi, config, state, f"Market status is {market.get('status')!r}, not open.")
        return

    positions = kalshi.positions(ticker)
    open_position = any(decimal_value(p.get("position_fp", p.get("position", 0))) != 0 for p in positions)
    if open_position:
        halt(kalshi, config, state, "A position exists in this market; canceling any remainder and stopping for manual review.")
        return

    ours, manual_bids = owned_orders(kalshi, ticker)
    if len(ours) > 1:
        halt(kalshi, config, state, f"Found {len(ours)} tagged resting orders; expected at most one.")
        return
    if manual_bids:
        halt(kalshi, config, state, "An untagged YES bid exists in this market; leaving it untouched and stopping.")
        return

    book = kalshi.orderbook(ticker)
    yes_levels = book.get("yes_dollars", [])
    no_levels = book.get("no_dollars", [])
    best_yes = best_price_cents(yes_levels)
    best_no = best_price_cents(no_levels)
    if best_yes is None or best_no is None:
        log("Order book is missing one side; no order change this run.")
        return

    ask_cents = 100 - best_no
    spread_cents = ask_cents - best_yes
    if state.get("start_bid_cents") is None:
        state["start_bid_cents"] = best_yes
    if abs(best_yes - int(state["start_bid_cents"])) > int(config["max_price_move_cents"]):
        halt(kalshi, config, state, f"Best YES bid moved from {state['start_bid_cents']}¢ to {best_yes}¢.")
        return
    if spread_cents > int(config["max_spread_cents"]):
        halt(kalshi, config, state, f"Spread is {spread_cents}¢, wider than the configured limit.")
        return

    target = min(best_yes, int(config["max_price_cents"]), ask_cents - 1)
    if target < int(config["min_price_cents"]):
        log(f"Best bid is below the minimum of {config['min_price_cents']}¢; no order change.")
        return
    count = (Decimal(str(config["max_spend_dollars"])) * 100 / Decimal(target)).to_integral_value(rounding=ROUND_FLOOR)
    cost = count * Decimal(target) / 100
    if count < 1:
        halt(kalshi, config, state, "Spend cap is too small for one contract at the target price.")
        return

    current = ours[0] if ours else None
    current_cents = None
    if current:
        current_cents = int((decimal_value(current.get("yes_price_dollars")) * 100).to_integral_value(rounding=ROUND_FLOOR))
    if current and current_cents is not None and current_cents >= target:
        log(f"Order is already at {current_cents}¢; current best bid {best_yes}¢, spread {spread_cents}¢.")
        return

    today_moves = int(state.get("moves_today", 0))
    if today_moves >= int(config["max_moves_per_utc_day"]):
        log("Daily order-move limit reached; leaving the resting order unchanged.")
        return

    if current:
        if live:
            log(f"Canceling own order {current['order_id']} before replacing it.")
            kalshi.cancel(current["order_id"], ticker)
            # Order reads can lag the matching engine; verify it is no longer resting before replacement.
            for _ in range(4):
                time.sleep(1)
                remaining = [o for o in kalshi.orders(ticker) if o.get("order_id") == current["order_id"]]
                if not remaining:
                    break
            else:
                raise RuntimeError("Could not verify cancellation; refusing to place a second order.")
        else:
            log(f"DRY RUN: would cancel own order {current['order_id']} before replacing it.")

    if live:
        available_cash = kalshi.balance_dollars()
        if available_cash < cost:
            halt(kalshi, config, state, f"Available cash ${available_cash:.2f} is below the order cost ${cost:.2f}.")
            return

    log(f"{'LIVE' if live else 'DRY RUN'}: BUY YES {count} contracts at {target}¢; max spend ${cost:.2f}; post-only.")
    if live:
        expiration = int(stop_time.timestamp())
        response = kalshi.create_yes_bid(ticker, count, target, expiration)
        if decimal_value(response.get("fill_count", "0")) > 0:
            halt(kalshi, config, state, "The create response reported an immediate fill; stopping for manual position review.")
            return
        if decimal_value(response.get("remaining_count", "0")) <= 0:
            halt(kalshi, config, state, "The create response did not leave a resting order.")
            return
    if live:
        state["moves_today"] = today_moves + 1


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {
        "start_bid_cents": None,
        "moves_today": 0,
        "utc_day": None,
        "error_count": 0,
        "halted": False,
        "halt_reason": None,
        "heartbeat_day": None,
    })
    live = os.environ.get("KALSHI_LIVE", "false").strip().lower() == "true"
    ticker = str(config.get("ticker", "")).strip()
    stop_at = str(config.get("stop_at_utc", "")).strip()
    if not ticker or ticker.startswith("REPLACE_") or not stop_at or stop_at.startswith("REPLACE_"):
        log("Setup is incomplete. Set an eligible market ticker and its reward end time in config.json.")
        return 0
    if not os.environ.get("KALSHI_API_KEY_ID") or not os.environ.get("KALSHI_PRIVATE_KEY_PEM"):
        log("Waiting for Kalshi API key secrets to be added in GitHub Actions settings.")
        return 0
    kalshi = None
    try:
        kalshi = Kalshi()
        log(f"{'LIVE' if live else 'DRY RUN'} one-shot check; the $20 cap and guardrails are in config.json.")
        run_once(config, state, kalshi, live)
        state["error_count"] = 0
    except StopRun as exc:
        log(f"NOT RUNNING: {exc}")
        save_state(state)
        return 0
    except Exception as exc:
        state["error_count"] = int(state.get("error_count", 0)) + 1
        log(f"Check failed ({state['error_count']}/{config.get('max_errors_in_a_row', 5)}): {exc}")
        if state["error_count"] >= int(config.get("max_errors_in_a_row", 5)):
            state["halted"] = True
            state["halt_reason"] = "Repeated API or processing errors."
            if kalshi is not None:
                try:
                    cancel_ours(kalshi, config.get("ticker", ""))
                except Exception as cancel_error:
                    log(f"Cancellation attempt failed: {cancel_error}. Check Kalshi → Portfolio → Orders.")
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
