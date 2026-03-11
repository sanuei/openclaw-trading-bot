#!/usr/bin/env python3
"""
OpenClaw live trading engine.

Goals:
- Read and update strategy.json dynamically.
- Produce more frequent, more watchable trades for livestreaming.
- Keep the existing dashboard files intact: trades.json, thinking.json, status.json.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path("/Users/sonic/.openclaw/workspace/trading-bot")
TRADE_LOG = BASE_DIR / "trades.json"
THINK_FILE = BASE_DIR / "thinking.json"
STATUS_FILE = BASE_DIR / "status.json"
STRATEGY_FILE = BASE_DIR / "strategy.json"

BASE_URL = "https://fapi.binance.com"
USER_AGENT = "openclaw-live-strategy/2.0"

MAJOR_COINS = [
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "BNB",
]

DEFAULT_STRATEGY: dict[str, Any] = {
    "coins": MAJOR_COINS[:],
    "coinUniverse": MAJOR_COINS[:],
    "takeProfit": 0,
    "stopLoss": 50,
    "positionSize": 100,
    "capitalFraction": 1.0,
    "leverage": 10,
    "entryThresholdPct": 0.48,
    "reversalThresholdPct": 0.34,
    "maxHoldMinutes": 90,
    "cooldownMinutes": 1,
    "targetTradesPerHour": 8,
    "maxConcurrentPositions": 1,
    "preferredDirection": "long",
    "dynamicAdjustment": True,
    "dynamicCoinSelection": True,
    "profitPriority": True,
    "allInMode": True,
    "allInBuffer": 0.985,
    "volumeTopN": 8,
    "gainersTopN": 5,
    "softStopLossPct": 1.6,
    "hardStopBalanceLossPct": 50,
    "trailActivationPct": 1.4,
    "profitLockDropPct": 0.65,
    "minQuoteVolume": 150000000,
    "minHourlyVolatilityPct": 0.2,
    "minVolumeRatio": 1.05,
    "volumeTrendWeight": 0.7,
    "minTrendStrengthPct": 0.15,
    "strategyResetAt": "",
    "mode": "major-liquid-all-in",
}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_major_coin(symbol: str) -> bool:
    base = str(symbol).upper().removesuffix("USDT")
    return base in MAJOR_COINS


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")

API_KEY = os.getenv("BINANCE_API_KEY", "")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")


def ensure_credentials() -> None:
    if API_KEY and SECRET_KEY:
        return
    raise RuntimeError("Missing Binance API credentials. Copy .env.example to .env and fill BINANCE_API_KEY / BINANCE_SECRET_KEY.")


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct_change(current: float, previous: float) -> float:
    if not previous:
        return 0.0
    return ((current - previous) / previous) * 100.0


def calculate_rsi(prices: list[float], period: int = 14) -> float:
    """计算RSI指标"""
    if len(prices) < period + 1:
        return 50.0  # 数据不足返回中性值
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def step_round(value: float, step: float) -> float:
    if step <= 0:
        return value
    quantized = Decimal(str(value)).quantize(Decimal(str(step)), rounding=ROUND_DOWN)
    return float(quantized)


def minutes_since(ts: str | None) -> float:
    if not ts:
        return 10**9
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 10**9
    return (datetime.now() - dt).total_seconds() / 60.0


def direction_label(direction: str) -> str:
    return "做空" if str(direction).lower() == "short" else "做多"


def load_strategy() -> dict[str, Any]:
    strategy = read_json(STRATEGY_FILE, {})
    if not isinstance(strategy, dict):
        strategy = {}

    # 排除 lastAdaptation，避免它覆盖用户的交易设置
    if "lastAdaptation" in strategy:
        del strategy["lastAdaptation"]

    merged = {**DEFAULT_STRATEGY, **strategy}
    merged["coins"] = [str(item).upper() for item in merged.get("coins", []) if item]
    merged["coinUniverse"] = [str(item).upper() for item in merged.get("coinUniverse", []) if item]
    if not merged["coins"]:
        merged["coins"] = DEFAULT_STRATEGY["coins"][:]
    if not merged["coinUniverse"]:
        merged["coinUniverse"] = DEFAULT_STRATEGY["coinUniverse"][:]

    merged["takeProfit"] = clamp(to_float(merged.get("takeProfit"), 0), 0, 12.0)
    merged["stopLoss"] = clamp(to_float(merged.get("stopLoss"), 50), 1, 100)
    merged["positionSize"] = clamp(to_float(merged.get("positionSize"), 100), 5, 100)
    merged["capitalFraction"] = clamp(to_float(merged.get("capitalFraction"), 1.0), 0.01, 1.0)
    merged["leverage"] = int(clamp(to_float(merged.get("leverage"), 10), 1, 125))
    merged["entryThresholdPct"] = clamp(to_float(merged.get("entryThresholdPct"), 0.48), 0.1, 2.0)
    merged["reversalThresholdPct"] = clamp(to_float(merged.get("reversalThresholdPct"), 0.34), 0.1, 2.0)
    merged["maxHoldMinutes"] = int(clamp(to_float(merged.get("maxHoldMinutes"), 90), 10, 1440))
    merged["cooldownMinutes"] = int(clamp(to_float(merged.get("cooldownMinutes"), 1), 0, 30))
    merged["targetTradesPerHour"] = int(clamp(to_float(merged.get("targetTradesPerHour"), 8), 1, 60))
    merged["maxConcurrentPositions"] = int(clamp(to_float(merged.get("maxConcurrentPositions"), 1), 1, 3))
    merged["minQuoteVolume"] = clamp(to_float(merged.get("minQuoteVolume"), 150000000), 0, 2000000000)
    merged["minHourlyVolatilityPct"] = clamp(to_float(merged.get("minHourlyVolatilityPct"), 0.2), 0.05, 10.0)
    merged["minVolumeRatio"] = clamp(to_float(merged.get("minVolumeRatio"), 1.05), 0.1, 10.0)
    merged["volumeTrendWeight"] = clamp(to_float(merged.get("volumeTrendWeight"), 0.7), 0.1, 3.0)
    merged["minTrendStrengthPct"] = clamp(to_float(merged.get("minTrendStrengthPct"), 0.15), 0.01, 5.0)
    merged["softStopLossPct"] = clamp(to_float(merged.get("softStopLossPct"), 1.6), 0.2, 20.0)
    merged["hardStopBalanceLossPct"] = clamp(to_float(merged.get("hardStopBalanceLossPct"), 50), 5, 90)
    merged["trailActivationPct"] = clamp(to_float(merged.get("trailActivationPct"), 1.4), 0.2, 20.0)
    merged["profitLockDropPct"] = clamp(to_float(merged.get("profitLockDropPct"), 0.65), 0.1, 10.0)
    merged["allInBuffer"] = clamp(to_float(merged.get("allInBuffer"), 0.72), 0.2, 1.0)
    merged["volumeTopN"] = int(clamp(to_float(merged.get("volumeTopN"), 8), 3, 15))
    merged["preferredDirection"] = str(merged.get("preferredDirection", "long")).lower()
    merged["mode"] = str(merged.get("mode", "major-liquid-all-in")).lower()
    merged["dynamicAdjustment"] = bool(merged.get("dynamicAdjustment", True))
    merged["dynamicCoinSelection"] = bool(merged.get("dynamicCoinSelection", True))
    merged["profitPriority"] = bool(merged.get("profitPriority", True))
    merged["allInMode"] = bool(merged.get("allInMode", True))
    merged["strategyResetAt"] = str(merged.get("strategyResetAt") or now_str())

    write_json(STRATEGY_FILE, merged)
    return merged


def normalize_strategy_values(strategy: dict[str, Any]) -> dict[str, Any]:
    strategy["takeProfit"] = round(to_float(strategy.get("takeProfit"), 0), 2)
    strategy["stopLoss"] = round(to_float(strategy.get("stopLoss"), 50), 2)
    strategy["positionSize"] = round(to_float(strategy.get("positionSize"), 100), 2)
    strategy["capitalFraction"] = round(to_float(strategy.get("capitalFraction"), 1.0), 3)
    strategy["entryThresholdPct"] = round(to_float(strategy.get("entryThresholdPct"), 0.48), 2)
    strategy["reversalThresholdPct"] = round(to_float(strategy.get("reversalThresholdPct"), 0.34), 2)
    strategy["minQuoteVolume"] = round(to_float(strategy.get("minQuoteVolume"), 150000000), 2)
    strategy["minHourlyVolatilityPct"] = round(to_float(strategy.get("minHourlyVolatilityPct"), 0.2), 2)
    strategy["minVolumeRatio"] = round(to_float(strategy.get("minVolumeRatio"), 1.05), 2)
    strategy["volumeTrendWeight"] = round(to_float(strategy.get("volumeTrendWeight"), 0.7), 2)
    strategy["minTrendStrengthPct"] = round(to_float(strategy.get("minTrendStrengthPct"), 0.15), 2)
    strategy["softStopLossPct"] = round(to_float(strategy.get("softStopLossPct"), 1.6), 2)
    strategy["hardStopBalanceLossPct"] = round(to_float(strategy.get("hardStopBalanceLossPct"), 50), 2)
    strategy["trailActivationPct"] = round(to_float(strategy.get("trailActivationPct"), 1.4), 2)
    strategy["profitLockDropPct"] = round(to_float(strategy.get("profitLockDropPct"), 0.65), 2)
    strategy["allInBuffer"] = round(to_float(strategy.get("allInBuffer"), 0.985), 3)
    strategy["volumeTopN"] = int(to_float(strategy.get("volumeTopN"), 8))
    strategy["leverage"] = int(to_float(strategy.get("leverage"), 10))
    strategy["maxHoldMinutes"] = int(to_float(strategy.get("maxHoldMinutes"), 90))
    strategy["cooldownMinutes"] = int(to_float(strategy.get("cooldownMinutes"), 1))
    strategy["targetTradesPerHour"] = int(to_float(strategy.get("targetTradesPerHour"), 8))
    strategy["maxConcurrentPositions"] = int(to_float(strategy.get("maxConcurrentPositions"), 1))
    return strategy


class BinanceClient:
    def __init__(self, api_key: str, secret_key: str, dry_run: bool = False) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.dry_run = dry_run
        self._exchange_info: dict[str, Any] | None = None

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params)
        return hmac.new(self.secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()

    def private_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = self._sign(params)
        query = urlencode(params)
        url = f"{BASE_URL}{path}?{query}"
        headers = {"X-MBX-APIKEY": self.api_key}
        return self._request(method, url, headers=headers)

    def public_request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode(params or {})
        url = f"{BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"
        return self._request("GET", url, headers={})

    def _request(self, method: str, url: str, headers: dict[str, str]) -> Any:
        merged_headers = {"User-Agent": USER_AGENT, **headers}
        request = Request(url, method=method, headers=merged_headers)
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"network error: {exc}") from exc

    def get_balance(self) -> float:
        return self.get_balance_info().get("balance", 0.0)

    def get_balance_info(self) -> dict[str, float]:
        payload = self.private_request("GET", "/fapi/v2/balance")
        for item in payload:
            if item.get("asset") == "USDT":
                return {
                    "balance": to_float(item.get("balance")),
                    "availableBalance": to_float(item.get("availableBalance")),
                    "crossWalletBalance": to_float(item.get("crossWalletBalance")),
                    "crossUnPnl": to_float(item.get("crossUnPnl")),
                }
        return {
            "balance": 0.0,
            "availableBalance": 0.0,
            "crossWalletBalance": 0.0,
            "crossUnPnl": 0.0,
        }

    def get_positions(self) -> list[dict[str, Any]]:
        payload = self.private_request("GET", "/fapi/v2/account")
        positions = []
        for item in payload.get("positions", []):
            amount = to_float(item.get("positionAmt"))
            if abs(amount) <= 0:
                continue
            direction = "long" if amount > 0 else "short"
            positions.append(
                {
                    "symbol": item["symbol"],
                    "amount": abs(amount),
                    "raw_amount": amount,
                    "direction": direction,
                    "entryPrice": to_float(item.get("entryPrice")),
                    "markPrice": to_float(item.get("markPrice")),
                    "unrealizedProfit": to_float(item.get("unrealizedProfit")),
                    "leverage": int(to_float(item.get("leverage"), 0)),
                }
            )
        return positions

    def get_symbol_ticker(self, symbol: str) -> dict[str, Any]:
        return self.public_request("/fapi/v1/ticker/24hr", {"symbol": symbol})

    def get_all_tickers(self) -> list[dict[str, Any]]:
        payload = self.public_request("/fapi/v1/ticker/24hr")
        return payload if isinstance(payload, list) else []

    def get_price(self, symbol: str) -> float:
        payload = self.public_request("/fapi/v1/ticker/price", {"symbol": symbol})
        return to_float(payload.get("price"))

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 12) -> list[list[Any]]:
        return self.public_request("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        if self.dry_run:
            return {"symbol": symbol, "leverage": leverage, "dryRun": True}
        return self.private_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def create_order(self, symbol: str, side: str, quantity: float, position_side: str) -> Any:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side,
        }
        if self.dry_run:
            return {"orderId": "dry-run", **params}
        return self.private_request("POST", "/fapi/v1/order", params)

    def create_stop_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        position_side: str,
        stop_price: float,
    ) -> Any:
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "quantity": quantity,
            "positionSide": position_side,
            "triggerPrice": stop_price,
            "workingType": "CONTRACT_PRICE",
            "priceProtect": "TRUE",
        }
        if self.dry_run:
            return {"algoId": "dry-run-stop", **params}
        return self.private_request("POST", "/fapi/v1/algoOrder", params)

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        payload = self.private_request("GET", "/fapi/v1/openOrders", params)
        return payload if isinstance(payload, list) else []

    def cancel_order(self, symbol: str, order_id: int) -> Any:
        if self.dry_run:
            return {"symbol": symbol, "orderId": order_id, "dryRun": True}
        return self.private_request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def cancel_open_orders(self, symbol: str) -> list[Any]:
        orders = self.get_open_orders(symbol)
        results = []
        for order in orders:
            order_id = order.get("orderId")
            if order_id is None:
                continue
            results.append(self.cancel_order(symbol, int(order_id)))
        return results

    def get_open_algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        payload = self.private_request("GET", "/fapi/v1/openAlgoOrders", params)
        return payload if isinstance(payload, list) else []

    def cancel_open_algo_orders(self, symbol: str) -> Any:
        if self.dry_run:
            return {"symbol": symbol, "dryRun": True}
        return self.private_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    def exchange_info(self) -> dict[str, Any]:
        if self._exchange_info is None:
            self._exchange_info = self.public_request("/fapi/v1/exchangeInfo")
        return self._exchange_info

    def symbol_meta(self, symbol: str) -> dict[str, Any]:
        info = self.exchange_info()
        for item in info.get("symbols", []):
            if item.get("symbol") == symbol:
                return item
        raise KeyError(f"symbol metadata not found: {symbol}")


class EventLogger:
    def __init__(self, keep_thoughts: int = 60) -> None:
        self.keep_thoughts = keep_thoughts

    def thinking(self, message: str) -> None:
        thoughts = read_json(THINK_FILE, [])
        if not isinstance(thoughts, list):
            thoughts = []
        thoughts.append({"time": now_str(), "thought": message})
        write_json(THINK_FILE, thoughts[-self.keep_thoughts :])

    def trade(
        self,
        trade_type: str,
        symbol: str,
        amount: float,
        price: float,
        pnl: float,
        reason: str,
        direction: str,
        leverage: int,
        balance: float,
        trade_action: str,
    ) -> None:
        trades = read_json(TRADE_LOG, [])
        if not isinstance(trades, list):
            trades = []
        trades.append(
            {
                "time": now_str(),
                "type": trade_type,
                "symbol": symbol,
                "amount": amount,
                "price": price,
                "pnl": pnl,
                "reason": reason,
                "direction": direction,
                "leverage": leverage,
                "balance": balance,
                "tradeAction": trade_action,
            }
        )
        write_json(TRADE_LOG, trades)

    def status(self, payload: dict[str, Any]) -> None:
        write_json(STATUS_FILE, payload)


@dataclass
class Candidate:
    symbol: str
    coin: str
    direction: str
    score: float
    price: float
    day_change_pct: float
    momentum_1m: float
    momentum_3m: float
    momentum_5m: float
    trend_15m: float
    range_pct: float
    volume_ratio: float
    volume_trend_pct: float
    volume_consistency: int
    quote_volume: float
    rank: int
    reason: str


def load_trades() -> list[dict[str, Any]]:
    data = read_json(TRADE_LOG, [])
    return data if isinstance(data, list) else []


def trade_action(trade: dict[str, Any]) -> str:
    action = str(trade.get("tradeAction", "")).upper()
    if action in {"OPEN", "CLOSE"}:
        return action

    trade_type = str(trade.get("type", "")).upper()
    direction = str(trade.get("direction", "long")).lower()

    if direction == "short":
        if trade_type == "SELL":
            return "OPEN"
        if trade_type == "BUY":
            return "CLOSE"
    else:
        if trade_type == "BUY":
            return "OPEN"
        if trade_type == "SELL":
            return "CLOSE"
    return "UNKNOWN"


def open_trade_map(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    opened: dict[str, dict[str, Any]] = {}
    for trade in trades:
        symbol = trade.get("symbol")
        direction = str(trade.get("direction", "long")).lower()
        if not symbol:
            continue
        key = f"{symbol}:{direction}"
        action = trade_action(trade)
        if action == "OPEN":
            opened[key] = trade
        elif action == "CLOSE":
            opened.pop(key, None)
    return opened


def parse_local_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def recent_closed_trades(trades: list[dict[str, Any]], hours: int = 6, since: str | None = None) -> list[dict[str, Any]]:
    cutoff = time.time() - hours * 3600
    reset_ts = parse_local_timestamp(since)
    recent = []
    for trade in trades:
        if trade_action(trade) != "CLOSE":
            continue
        ts = parse_local_timestamp(trade.get("time"))
        if ts is None:
            continue
        if reset_ts is not None and ts < reset_ts:
            continue
        if ts >= cutoff:
            recent.append(trade)
    return recent


def trade_return(trade: dict[str, Any]) -> float:
    pnl = to_float(trade.get("pnl"))
    ending_balance = to_float(trade.get("balance"))
    starting_balance = ending_balance - pnl
    if abs(starting_balance) < 1e-9:
        return 0.0
    return pnl / starting_balance


def sharpe_ratio(trades: list[dict[str, Any]]) -> float:
    returns = [trade_return(trade) for trade in trades]
    if not returns:
        return 0.0
    mean_return = sum(returns) / len(returns)
    variance = sum((item - mean_return) ** 2 for item in returns) / len(returns)
    std_dev = math.sqrt(variance)
    if std_dev < 1e-9:
        if mean_return > 0:
            return 3.0
        if mean_return < 0:
            return -3.0
        return 0.0
    return (mean_return / std_dev) * math.sqrt(len(returns))


def adapt_strategy(strategy: dict[str, Any], trades: list[dict[str, Any]], logger: EventLogger) -> tuple[dict[str, Any], list[str]]:
    # 在函数开头保存用户设置
    user_settings = {
        "capitalFraction": strategy.get("capitalFraction"),
        "allInMode": strategy.get("allInMode"),
        "leverage": strategy.get("leverage"),
        "takeProfit": strategy.get("takeProfit"),
        "stopLoss": strategy.get("stopLoss"),
        "positionSize": strategy.get("positionSize"),
    }
    
    if not strategy.get("dynamicAdjustment", True):
        strategy["lastAdaptation"] = {
            "time": now_str(),
            "recentClosedTrades": len(recent_closed_trades(trades, hours=6, since=strategy.get("strategyResetAt"))),
            "changes": [],
            "mode": "fixed-major-liquid-all-in",
        }
        return strategy, []

    recent = recent_closed_trades(trades, hours=6, since=strategy.get("strategyResetAt"))
    recent_2h = recent_closed_trades(trades, hours=2, since=strategy.get("strategyResetAt"))
    wins = [trade for trade in recent if to_float(trade.get("pnl")) > 0]
    recent_count = len(recent_2h)
    changes: list[str] = []

    losing_streak = 0
    for trade in reversed(recent):
        if to_float(trade.get("pnl")) < 0:
            losing_streak += 1
        else:
            break

    win_rate = len(wins) / len(recent) if recent else 0.0
    avg_pnl = sum(to_float(trade.get("pnl")) for trade in recent) / len(recent) if recent else 0.0
    avg_return = sum(trade_return(trade) for trade in recent) / len(recent) if recent else 0.0
    sharpe = sharpe_ratio(recent)

    if len(recent) >= 3 and (sharpe < 0 or (win_rate < 0.45 and avg_pnl < 0) or losing_streak >= 2):
        new_threshold = clamp(strategy["entryThresholdPct"] + 0.05, 0.2, 1.2)
        new_stop = clamp(strategy["softStopLossPct"] - 0.15, 0.6, 4.0)
        new_trail = clamp(strategy["trailActivationPct"] - 0.1, 0.8, 4.0)
        new_cooldown = min(8, strategy["cooldownMinutes"] + 1)

        if new_threshold != strategy["entryThresholdPct"]:
            strategy["entryThresholdPct"] = new_threshold
            changes.append(f"提高入场阈值到 {new_threshold:.2f}")
        if new_stop != strategy["softStopLossPct"]:
            strategy["softStopLossPct"] = new_stop
            strategy["stopLoss"] = strategy["hardStopBalanceLossPct"]
            changes.append(f"止损收紧到 {new_stop:.2f}%")
        if new_trail != strategy["trailActivationPct"]:
            strategy["trailActivationPct"] = new_trail
        if new_cooldown != strategy["cooldownMinutes"]:
            strategy["cooldownMinutes"] = new_cooldown
            changes.append(f"冷却时间改为 {new_cooldown} 分钟")
        strategy["mode"] = "profit-defensive"
    elif len(recent) >= 3 and sharpe > 0.6 and win_rate >= 0.55 and avg_return > 0:
        new_threshold = clamp(strategy["entryThresholdPct"] - 0.03, 0.2, 1.0)
        new_stop = clamp(strategy["softStopLossPct"] + 0.08, 0.6, 4.0)
        new_hold = min(180, strategy["maxHoldMinutes"] + 10)

        if new_threshold != strategy["entryThresholdPct"]:
            strategy["entryThresholdPct"] = new_threshold
            changes.append(f"放宽入场阈值到 {new_threshold:.2f}")
        if new_stop != strategy["softStopLossPct"]:
            strategy["softStopLossPct"] = new_stop
            strategy["stopLoss"] = strategy["hardStopBalanceLossPct"]
            changes.append(f"止损放宽到 {new_stop:.2f}%")
        if new_hold != strategy["maxHoldMinutes"]:
            strategy["maxHoldMinutes"] = new_hold
            changes.append(f"最大持仓时长延长到 {new_hold} 分钟")
        strategy["mode"] = "profit-max"
    else:
        strategy["mode"] = "profit-balance"

    # 保留用户的交易设置，不强制重置
    # strategy["leverage"] = 10
    # strategy["takeProfit"] = 0
    # strategy["stopLoss"] = strategy["hardStopBalanceLossPct"]
    # strategy["allInMode"] = True
    # strategy["allInBuffer"] = 0.985
    # strategy["capitalFraction"] = 1.0
    # strategy["positionSize"] = 100.0
    # strategy["preferredDirection"] = "long"

    # 在 lastAdaptation 之前保存用户设置
    user_settings = {
        "capitalFraction": strategy.get("capitalFraction"),
        "allInMode": strategy.get("allInMode"),
        "leverage": strategy.get("leverage"),
        "takeProfit": strategy.get("takeProfit"),
        "stopLoss": strategy.get("stopLoss"),
        "positionSize": strategy.get("positionSize"),
    }

    strategy["lastAdaptation"] = {
        "time": now_str(),
        "recentClosedTrades": len(recent),
        "recentTwoHourTrades": recent_count,
        "winRate": round(win_rate, 3),
        "avgPnl": round(avg_pnl, 4),
        "avgReturn": round(avg_return, 4),
        "sharpe": round(sharpe, 4),
        "losingStreak": losing_streak,
        "changes": changes,
        "strategyResetAt": strategy.get("strategyResetAt"),
    }

    # 总是恢复用户设置的关键字段
    for key in ["capitalFraction", "allInMode", "leverage", "takeProfit", "stopLoss", "positionSize"]:
        if key in user_settings and user_settings[key] is not None:
            strategy[key] = user_settings[key]

    if changes:
        normalize_strategy_values(strategy)
        logger.thinking("🧬 动态调参: " + " | ".join(changes))
        write_json(STRATEGY_FILE, strategy)

    return strategy, changes


def symbol_is_affordable(
    client: BinanceClient,
    strategy: dict[str, Any],
    symbol: str,
    balance: float | None = None,
) -> bool:
    filters = get_symbol_filters(client, symbol)
    min_margin = filters["minNotional"] / max(strategy["leverage"], 1)
    if balance is None:
        balance = client.get_balance()
    budget = (
        balance * strategy.get("allInBuffer", 0.985)
        if strategy.get("allInMode", False)
        else balance * strategy.get("capitalFraction", 0.72)
    )
    return min_margin <= budget * 1.02


def select_watchlist(strategy: dict[str, Any], client: BinanceClient) -> list[str]:
    if not strategy.get("dynamicCoinSelection", True):
        return [coin.upper() for coin in strategy.get("coins", [])]

    allowed_symbols = {
        item.get("symbol"): item
        for item in client.exchange_info().get("symbols", [])
        if item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
        and is_major_coin(item.get("symbol", ""))
    }
    ranking: list[tuple[float, float, str]] = []

    for ticker in client.get_all_tickers():
        symbol = str(ticker.get("symbol") or "").upper()
        if symbol not in allowed_symbols:
            continue
        quote_volume = to_float(ticker.get("quoteVolume"))
        day_change = to_float(ticker.get("priceChangePercent"))
        ranking.append((quote_volume, day_change, symbol))

    ranking.sort(key=lambda item: (item[0], item[1]), reverse=True)
    major_limit = min(len(MAJOR_COINS), int(strategy.get("gainersTopN", len(MAJOR_COINS))) or len(MAJOR_COINS))
    picked = [symbol.removesuffix("USDT") for _, _, symbol in ranking[:major_limit]]
    if picked:
        strategy["coins"] = picked
        strategy["coinUniverse"] = picked[:]
        watchlist_user_settings = {
            "capitalFraction": strategy.get("capitalFraction"),
            "allInMode": strategy.get("allInMode"),
            "leverage": strategy.get("leverage"),
        }
        write_json(STRATEGY_FILE, strategy)
        # 恢复用户设置
        for key, val in watchlist_user_settings.items():
            if val is not None:
                strategy[key] = val
    return picked or MAJOR_COINS[:]


def build_candidates(strategy: dict[str, Any], client: BinanceClient, logger: EventLogger) -> list[Candidate]:
    candidates: list[Candidate] = []
    balance_info = client.get_balance_info()
    available_balance = balance_info.get("availableBalance") or balance_info.get("balance")
    preferred = strategy["preferredDirection"]

    logger.thinking("📡 主流币池扫描: " + " / ".join(strategy["coins"]))

    for rank, coin in enumerate(strategy["coins"], start=1):
        symbol = f"{coin}USDT"
        try:
            ticker = client.get_symbol_ticker(symbol)
            if not symbol_is_affordable(client, strategy, symbol, balance=available_balance):
                logger.thinking(f"💸 {symbol} 最低成交门槛过高，当前资金跳过")
                continue
            klines = client.get_klines(symbol, interval="1m", limit=15)
            trend_klines = client.get_klines(symbol, interval="5m", limit=8)
        except Exception as exc:
            logger.thinking(f"⚠️ {symbol} 行情读取失败: {exc}")
            continue

        if len(klines) < 8 or len(trend_klines) < 4:
            continue

        closes = [to_float(item[4]) for item in klines]
        highs = [to_float(item[2]) for item in klines]
        lows = [to_float(item[3]) for item in klines]
        volumes = [to_float(item[5]) for item in klines]
        trend_closes = [to_float(item[4]) for item in trend_klines]

        price = closes[-1]
        day_change = to_float(ticker.get("priceChangePercent"))
        quote_volume = to_float(ticker.get("quoteVolume"))
        momentum_1m = pct_change(closes[-1], closes[-2])
        momentum_3m = pct_change(closes[-1], closes[-4])
        momentum_5m = pct_change(trend_closes[-1], trend_closes[-2])
        trend_15m = pct_change(trend_closes[-1], trend_closes[-4])
        range_pct = ((max(highs[-8:]) - min(lows[-8:])) / price) * 100 if price else 0.0
        recent_average_volume = sum(volumes[-6:-1]) / max(1, len(volumes[-6:-1]))
        volume_ratio = volumes[-1] / recent_average_volume if recent_average_volume else 1.0
        volume_window = volumes[-6:]
        early_volume_avg = sum(volume_window[:3]) / 3 if len(volume_window) >= 3 else 0.0
        late_volume_avg = sum(volume_window[-3:]) / 3 if len(volume_window) >= 3 else 0.0
        volume_trend_pct = pct_change(late_volume_avg, early_volume_avg) if early_volume_avg else 0.0
        volume_consistency = sum(
            1
            for previous, current in zip(volume_window, volume_window[1:])
            if current >= previous * 0.98
        )
        volume_trend_score = (
            max(volume_trend_pct, 0) * 0.03
            + max(volume_consistency - 1, 0) * 0.18
        ) * strategy["volumeTrendWeight"]

        if quote_volume < strategy["minQuoteVolume"]:
            logger.thinking(f"🌫️ {symbol} 流动性不足，跳过")
            continue
        if volume_ratio < strategy["minVolumeRatio"]:
            logger.thinking(f"📉 {symbol} 量能不足 ({volume_ratio:.2f}x)，跳过")
            continue
        if range_pct < strategy["minHourlyVolatilityPct"]:
            logger.thinking(f"🧊 {symbol} 波动过低 ({range_pct:.2f}%)，跳过")
            continue

        # 计算 RSI
        rsi = calculate_rsi(closes, period=14)
        rsi_threshold = strategy.get("rsiThreshold", 70)
        
        # 计算日内低点和高点
        day_high = max(highs)
        day_low = min(lows)
        price_position = ((price - day_low) / (day_high - day_low) * 100) if day_high > day_low else 50
        
        # 计算简单均线
        sma_5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else price
        sma_10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else price
        
        # 回调买入条件：价格接近均线或从高点回落
        pullback_strength = (day_high - price) / day_high * 100 if day_high > 0 else 0  # 距离高点的跌幅
        near_ma = abs(price - sma_5) / sma_5 * 100  # 距离5日均线百分比
        is_pullback = pullback_strength > 0.5 and near_ma < 2.0  # 有回调且接近均线
        
        # RSI 过滤：RSI > 70 不买（超买）
        if rsi >= rsi_threshold:
            logger.thinking(f"🔴 {symbol} RSI={rsi:.1f} 超买，跳过")
            continue
        
        # 回调买入加分
        if is_pullback:
            logger.thinking(f"📉 {symbol} 检测到回调: 距高点 -{pullback_strength:.1f}%, 距均线 {near_ma:.2f}%")
        else:
            # 不是回调不买（除非是强势突破）
            if momentum_1m < 0.1 and rsi > 75:
                logger.thinking(f"⏸️ {symbol} 无回调且动量不足，跳过")
                continue

        long_valid = (
            day_change > 0
            # and
            # trend_15m >= strategy["minTrendStrengthPct"]
            # and momentum_5m > 0
            # and momentum_3m > 0
            # and momentum_1m >= -0.03
        )

        long_score = (
            max(trend_15m, 0) * 1.45
            + max(momentum_5m, 0) * 1.10
            + max(momentum_3m, 0) * 0.55
            + max(momentum_1m, 0) * 0.25
            + max(volume_ratio - 1.0, 0) * 0.35
            + volume_trend_score
            + max(day_change, 0) * 0.05
            + max(strategy["gainersTopN"] - rank + 1, 0) * 0.04
        )

        # 回调买入加分
        if is_pullback:
            long_score += 0.8  # 回调买入加高分
        
        # RSI 低于 50 加分（超卖反弹概率大）
        if rsi < 50:
            long_score += 0.3

        if preferred == "long":
            direction = "long"
            score = long_score if long_valid else 0.0
        else:
            direction = "long"
            score = long_score if long_valid else 0.0

        reason = (
            f"主流成交额第{rank}名 涨幅 {day_change:+.2f}% | RSI {rsi:.0f} | "
            f"1m {momentum_1m:+.2f}% | 3m {momentum_3m:+.2f}% | 5m {momentum_5m:+.2f}% | "
            f"15m趋势 {trend_15m:+.2f}% | 波动 {range_pct:.2f}% | "
            f"量能 {volume_ratio:.2f}x | 回调 {'✓' if is_pullback else '✗'}"
        )

        logger.thinking(f"   • {symbol} {direction_label(direction)} 分数 {score:.2f} | {reason}")
        if score <= 0:
            continue

        candidates.append(
            Candidate(
                symbol=symbol,
                coin=coin,
                direction=direction,
                score=score,
                price=price,
                day_change_pct=day_change,
                momentum_1m=momentum_1m,
                momentum_3m=momentum_3m,
                momentum_5m=momentum_5m,
                trend_15m=trend_15m,
                range_pct=range_pct,
                volume_ratio=volume_ratio,
                volume_trend_pct=volume_trend_pct,
                volume_consistency=volume_consistency,
                quote_volume=quote_volume,
                rank=rank,
                reason=reason,
            )
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def get_symbol_filters(client: BinanceClient, symbol: str) -> dict[str, float]:
    meta = client.symbol_meta(symbol)
    step_size = 0.001
    tick_size = 0.0001
    min_qty = 0.0
    min_notional = 5.0
    quantity_precision = int(meta.get("quantityPrecision", 3))
    price_precision = int(meta.get("pricePrecision", 4))

    for item in meta.get("filters", []):
        if item.get("filterType") in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
            step_size = to_float(item.get("stepSize"), step_size)
            min_qty = max(min_qty, to_float(item.get("minQty"), min_qty))
        if item.get("filterType") == "PRICE_FILTER":
            tick_size = to_float(item.get("tickSize"), tick_size)
        if item.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
            min_notional = max(min_notional, to_float(item.get("notional"), to_float(item.get("minNotional"), min_notional)))

    return {
        "stepSize": step_size,
        "tickSize": tick_size,
        "minQty": min_qty,
        "minNotional": min_notional,
        "quantityPrecision": quantity_precision,
        "pricePrecision": price_precision,
    }


def round_price(value: float, tick_size: float, direction: str) -> float:
    if tick_size <= 0:
        return value
    units = value / tick_size
    rounded_units = math.floor(units) if direction == "down" else math.ceil(units)
    return rounded_units * tick_size


def hard_stop_price(
    entry_price: float,
    amount: float,
    direction: str,
    equity_at_open: float,
    hard_stop_balance_loss_pct: float,
) -> float:
    if entry_price <= 0 or amount <= 0 or equity_at_open <= 0:
        raise ValueError("invalid stop inputs")
    max_loss = equity_at_open * (hard_stop_balance_loss_pct / 100.0)
    price_move = max_loss / amount
    if direction == "short":
        return entry_price + price_move
    return max(0.00000001, entry_price - price_move)


def has_protective_stop(open_orders: list[dict[str, Any]], position: dict[str, Any]) -> bool:
    expected_side = "SELL" if position["direction"] == "long" else "BUY"
    expected_position_side = "LONG" if position["direction"] == "long" else "SHORT"
    for order in open_orders:
        if str(order.get("algoType", "")).upper() != "CONDITIONAL":
            continue
        if order.get("side") != expected_side:
            continue
        if order.get("positionSide") != expected_position_side:
            continue
        if str(order.get("status", "NEW")).upper() in {"NEW", "PARTIALLY_FILLED"}:
            return True
    return False


def place_protective_stop(
    client: BinanceClient,
    logger: EventLogger,
    strategy: dict[str, Any],
    position: dict[str, Any],
    equity_at_open: float,
) -> tuple[bool, float | None]:
    filters = get_symbol_filters(client, position["symbol"])
    raw_stop_price = hard_stop_price(
        entry_price=position["entryPrice"],
        amount=position["amount"],
        direction=position["direction"],
        equity_at_open=equity_at_open,
        hard_stop_balance_loss_pct=strategy["hardStopBalanceLossPct"],
    )
    stop_price = round_price(
        raw_stop_price,
        filters["tickSize"],
        "down" if position["direction"] == "long" else "up",
    )
    stop_price = float(f"{stop_price:.{filters['pricePrecision']}f}")
    side = "SELL" if position["direction"] == "long" else "BUY"
    position_side = "LONG" if position["direction"] == "long" else "SHORT"

    try:
        result = client.create_stop_order(
            symbol=position["symbol"],
            side=side,
            quantity=position["amount"],
            position_side=position_side,
            stop_price=stop_price,
        )
    except Exception as exc:
        logger.thinking(f"❌ {position['symbol']} 止损挂单失败: {exc}")
        return False, None

    if "algoId" not in result and "orderId" not in result:
        logger.thinking(f"❌ {position['symbol']} 止损挂单失败: {result}")
        return False, None
    logger.thinking(
        f"🛡️ {position['symbol']} 已挂强制止损单 | 触发价 {stop_price:.8f} | "
        f"账户回撤线 {strategy['hardStopBalanceLossPct']:.0f}%"
    )
    return True, stop_price


def calc_quantity(
    client: BinanceClient,
    symbol: str,
    strategy: dict[str, Any],
    price: float,
    balance: float,
) -> float:
    filters = get_symbol_filters(client, symbol)
    margin_budget = (
        balance * strategy.get("allInBuffer", 0.985)
        if strategy.get("allInMode", False)
        else balance * strategy.get("capitalFraction", 0.72)
    )
    notional = margin_budget * strategy["leverage"]
    raw_qty = notional / price if price else 0.0
    qty = step_round(raw_qty, filters["stepSize"])
    qty = max(qty, filters["minQty"])

    if qty * price < filters["minNotional"]:
        min_qty_for_notional = filters["minNotional"] / price
        qty = step_round(max(qty, min_qty_for_notional), filters["stepSize"])

    precision = filters["quantityPrecision"]
    qty = float(f"{qty:.{precision}f}")
    margin_required = (qty * price) / max(strategy["leverage"], 1)
    if margin_required > margin_budget * 1.01:
        raise ValueError(f"{symbol} 保证金需求 {margin_required:.4f}U 超出预算 {margin_budget:.4f}U")
    if qty <= 0:
        raise ValueError(f"invalid quantity for {symbol}: {qty}")
    return qty


def peak_pnl_pct_since_open(
    client: BinanceClient,
    position: dict[str, Any],
    opened_at: str | None,
) -> float:
    hold_minutes = int(max(3, min(minutes_since(opened_at) + 2, 120)))
    try:
        klines = client.get_klines(position["symbol"], interval="1m", limit=hold_minutes)
    except Exception:
        return position.get("pnlPct", 0.0)

    if not klines:
        return position.get("pnlPct", 0.0)

    entry_price = position["entryPrice"]
    if position["direction"] == "short":
        lowest = min(to_float(item[3]) for item in klines)
        return max(0.0, pct_change(entry_price, lowest))

    highest = max(to_float(item[2]) for item in klines)
    return max(0.0, pct_change(highest, entry_price))


def close_reason(
    client: BinanceClient,
    position: dict[str, Any],
    candidate_map: dict[str, Candidate],
    strategy: dict[str, Any],
    opened_trade: dict[str, Any],
    current_equity: float,
) -> tuple[bool, str]:
    pnl_pct = position["pnlPct"]
    opened_at = opened_trade.get("time")
    hold_minutes = minutes_since(opened_at)
    open_balance = to_float(opened_trade.get("balance"), current_equity)
    hard_stop_equity = open_balance * (1 - strategy["hardStopBalanceLossPct"] / 100.0)

    if open_balance > 0 and current_equity <= hard_stop_equity:
        return True, f"账户净值回撤达到 {strategy['hardStopBalanceLossPct']:.0f}% 强平"
    if strategy["takeProfit"] > 0 and pnl_pct >= strategy["takeProfit"]:
        return True, f"止盈 {strategy['takeProfit']:.2f}%"
    if pnl_pct <= -strategy["softStopLossPct"]:
        return True, f"软止损 {strategy['softStopLossPct']:.2f}%"

    candidate = candidate_map.get(position["symbol"])
    current_watchlist = {f"{coin}USDT" for coin in strategy.get("coins", [])}
    if position["symbol"] not in current_watchlist and hold_minutes >= 3:
        return True, "跌出主流币监控池"

    peak_pnl = peak_pnl_pct_since_open(client, position, opened_at)
    if (
        peak_pnl >= strategy["trailActivationPct"]
        and pnl_pct > 0
        and (peak_pnl - pnl_pct) >= strategy["profitLockDropPct"]
    ):
        return True, f"动态止盈 峰值 {peak_pnl:.2f}% 回撤到 {pnl_pct:.2f}%"

    if candidate and hold_minutes >= 3:
        if candidate.momentum_3m <= -strategy["reversalThresholdPct"]:
            return True, f"短线转弱 3m {candidate.momentum_3m:+.2f}%"
        if candidate.trend_15m < 0 and pnl_pct > 0:
            return True, f"趋势走弱 15m {candidate.trend_15m:+.2f}%"

    if hold_minutes >= strategy["maxHoldMinutes"] and pnl_pct < strategy["trailActivationPct"]:
        return True, f"持仓过久 {strategy['maxHoldMinutes']} 分钟"
    return False, ""


def manage_positions(
    client: BinanceClient,
    logger: EventLogger,
    strategy: dict[str, Any],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    candidates: list[Candidate],
) -> tuple[list[dict[str, Any]], list[str]]:
    opened_map = open_trade_map(trades)
    candidate_map = {item.symbol: item for item in candidates}
    events: list[str] = []
    balance_info = client.get_balance_info()
    current_balance = balance_info.get("balance", 0.0)
    current_equity = current_balance + sum(to_float(item.get("unrealizedProfit")) for item in positions)

    for position in positions:
        symbol = position["symbol"]
        direction = position["direction"]
        key = f"{symbol}:{direction}"
        opened_trade = opened_map.get(key, {})
        opened_at = opened_trade.get("time")
        equity_at_open = to_float(opened_trade.get("balance"), current_equity)
        current_price = position["markPrice"] or client.get_price(symbol)
        signed_move = current_price - position["entryPrice"]
        if direction == "short":
            signed_move *= -1
        pnl_pct = pct_change(current_price, position["entryPrice"])
        if direction == "short":
            pnl_pct *= -1

        position["pnlPct"] = pnl_pct

        logger.thinking(
            f"📈 <b>{symbol}</b> {direction_label(direction)} | 入场 {position['entryPrice']:.4f} | "
            f"现价 {current_price:.4f} | 浮盈 {pnl_pct:+.2f}% | 持仓 {minutes_since(opened_at):.1f} 分钟"
        )

        open_orders = client.get_open_algo_orders(symbol)
        if not has_protective_stop(open_orders, position):
            stop_ok, _ = place_protective_stop(client, logger, strategy, position, equity_at_open)
            if not stop_ok:
                should_close = True
                reason = "无法补挂止损单，强制平仓"
                side = "SELL" if direction == "long" else "BUY"
                position_side = "LONG" if direction == "long" else "SHORT"
                logger.thinking(f"🚨 {symbol} {reason}")
                client.cancel_open_algo_orders(symbol)
                client.cancel_open_orders(symbol)
                result = client.create_order(symbol, side, position["amount"], position_side)
                if "orderId" in result:
                    balance = client.get_balance()
                    realized = position["unrealizedProfit"] if abs(position["unrealizedProfit"]) > 0 else signed_move * position["amount"]
                    if not client.dry_run:
                        logger.trade(
                            trade_type=side,
                            symbol=symbol,
                            amount=position["amount"],
                            price=current_price,
                            pnl=realized,
                            reason=reason,
                            direction=direction,
                            leverage=position.get("leverage") or strategy["leverage"],
                            balance=balance,
                            trade_action="CLOSE",
                        )
                    events.append(f"{symbol} closed: {reason}")
                else:
                    events.append(f"{symbol} close failed")
                continue

        should_close, reason = close_reason(client, position, candidate_map, strategy, opened_trade, current_equity)
        if not should_close:
            continue

        side = "SELL" if direction == "long" else "BUY"
        position_side = "LONG" if direction == "long" else "SHORT"
        logger.thinking(f"🚪 {symbol} 触发离场: {reason}")
        client.cancel_open_algo_orders(symbol)
        client.cancel_open_orders(symbol)

        result = client.create_order(symbol, side, position["amount"], position_side)
        if "orderId" not in result:
            logger.thinking(f"❌ {symbol} 平仓失败: {result}")
            events.append(f"{symbol} close failed")
            continue

        balance = client.get_balance()
        realized = position["unrealizedProfit"] if abs(position["unrealizedProfit"]) > 0 else signed_move * position["amount"]
        if client.dry_run:
            logger.thinking(f"🧪 dry-run 模拟平仓 {symbol} | {reason} | 预计 {realized:+.4f}U")
            events.append(f"{symbol} dry-run close")
            continue
        logger.trade(
            trade_type=side,
            symbol=symbol,
            amount=position["amount"],
            price=current_price,
            pnl=realized,
            reason=reason,
            direction=direction,
            leverage=position.get("leverage") or strategy["leverage"],
            balance=balance,
            trade_action="CLOSE",
        )
        logger.thinking(f"✅ {symbol} 平仓完成 | {reason} | 实现 {realized:+.4f}U")
        events.append(f"{symbol} closed: {reason}")

    refreshed = client.get_positions()
    return refreshed, events


def last_trade_time(trades: list[dict[str, Any]]) -> str | None:
    if not trades:
        return None
    return trades[-1].get("time")


def enter_trade(
    client: BinanceClient,
    logger: EventLogger,
    strategy: dict[str, Any],
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    candidates: list[Candidate],
) -> str:
    if len(positions) >= strategy["maxConcurrentPositions"]:
        return "position cap reached"

    if not candidates:
        logger.thinking("😴 没有可执行信号，等待下一轮扫描")
        return "no candidate"

    if candidates[0].score < strategy["entryThresholdPct"]:
        logger.thinking(
            f"😴 最佳信号 {candidates[0].symbol} {direction_label(candidates[0].direction)} 分数 {candidates[0].score:.2f} "
            f"低于阈值 {strategy['entryThresholdPct']:.2f}"
        )
        return "score below threshold"

    cooldown = minutes_since(last_trade_time(trades))
    if cooldown < strategy["cooldownMinutes"]:
        logger.thinking(f"⏱️ 冷却中: 距离上一笔交易 {cooldown:.1f} 分钟")
        return "cooldown"

    balance_info = client.get_balance_info()
    balance = balance_info.get("balance", 0.0)
    available_balance = balance_info.get("availableBalance") or balance
    best: Candidate | None = None
    quantity = 0.0

    for candidate in candidates:
        if candidate.score < strategy["entryThresholdPct"]:
            break
        try:
            candidate_qty = calc_quantity(client, candidate.symbol, strategy, candidate.price, available_balance)
        except Exception as exc:
            logger.thinking(f"⚠️ {candidate.symbol} 数量计算失败: {exc}")
            continue

        estimated_margin = (candidate_qty * candidate.price) / max(strategy["leverage"], 1)
        if estimated_margin > available_balance * strategy.get("allInBuffer", 0.985) * 1.01:
            logger.thinking(
                f"💸 {candidate.symbol} 预计占用保证金 {estimated_margin:.2f}U，超出可用余额 {available_balance:.2f}U"
            )
            continue

        best = candidate
        quantity = candidate_qty
        break

    if best is None:
        logger.thinking("😴 候选币种都不满足保证金预算或最小下单门槛")
        return "no affordable candidate"

    side = "BUY" if best.direction == "long" else "SELL"
    position_side = "LONG" if best.direction == "long" else "SHORT"

    client.set_leverage(best.symbol, strategy["leverage"])
    logger.thinking(
        f"⚡ 准备全仓开仓 {best.symbol} {direction_label(best.direction)} | 数量 {quantity} | "
        f"资金占比 {strategy['capitalFraction'] * 100:.0f}% | 账户 {available_balance:.4f}U | "
        f"杠杆 {strategy['leverage']}x | {best.reason}"
    )

    filters = get_symbol_filters(client, best.symbol)
    result: dict[str, Any] = {}
    for _ in range(4):
        try:
            result = client.create_order(best.symbol, side, quantity, position_side)
        except RuntimeError as exc:
            message = str(exc)
            if "-2019" not in message and "Margin is insufficient" not in message:
                logger.thinking(f"❌ 开仓失败: {message}")
                return "open failed"

            next_quantity = step_round(quantity * 0.97, filters["stepSize"])
            next_quantity = float(f"{next_quantity:.{filters['quantityPrecision']}f}")
            if (
                next_quantity <= 0
                or next_quantity == quantity
                or next_quantity < filters["minQty"]
                or next_quantity * best.price < filters["minNotional"]
            ):
                logger.thinking(f"❌ 开仓失败: {message}")
                return "open failed"

            logger.thinking(
                f"⚠️ {best.symbol} 保证金不足，数量从 {quantity} 下调到 {next_quantity} 后重试"
            )
            quantity = next_quantity
            continue

        if "orderId" in result:
            break

    if "orderId" not in result:
        logger.thinking(f"❌ 开仓失败: {result}")
        return "open failed"

    entry_positions = [item for item in client.get_positions() if item["symbol"] == best.symbol and item["direction"] == best.direction]
    position_snapshot = entry_positions[0] if entry_positions else {
        "symbol": best.symbol,
        "direction": best.direction,
        "amount": quantity,
        "entryPrice": best.price,
        "markPrice": best.price,
        "unrealizedProfit": 0.0,
        "leverage": strategy["leverage"],
    }

    if client.dry_run:
        stop_ok, stop_price = place_protective_stop(client, logger, strategy, position_snapshot, balance)
        if not stop_ok:
            logger.thinking(f"❌ dry-run 止损模拟失败，放弃 {best.symbol} 开仓")
            return "dry-run stop failed"
        stop_text = f"{stop_price:.8f}" if stop_price is not None else "--"
        logger.thinking(
            f"🧪 dry-run 模拟开仓 {best.symbol} {direction_label(best.direction)} | "
            f"数量 {quantity} | 止损价 {stop_text}"
        )
        return f"dry-run opened {best.symbol} {best.direction}"

    client.cancel_open_algo_orders(best.symbol)
    client.cancel_open_orders(best.symbol)
    stop_ok, stop_price = place_protective_stop(client, logger, strategy, position_snapshot, balance)
    if not stop_ok:
        emergency_side = "SELL" if best.direction == "long" else "BUY"
        emergency_position_side = "LONG" if best.direction == "long" else "SHORT"
        logger.thinking(f"🚨 {best.symbol} 无法挂止损单，立即执行紧急平仓")
        client.create_order(best.symbol, emergency_side, position_snapshot["amount"], emergency_position_side)
        return "open reverted no stop"

    balance = client.get_balance()
    stop_text = f"{stop_price:.8f}" if stop_price is not None else "--"
    logger.trade(
        trade_type=side,
        symbol=best.symbol,
        amount=quantity,
        price=best.price,
        pnl=0.0,
        reason=f"主流币池全仓{direction_label(best.direction)} | 止损 {stop_text} | {best.reason}",
        direction=best.direction,
        leverage=strategy["leverage"],
        balance=balance,
        trade_action="OPEN",
        )
    logger.thinking(f"✅ 开仓完成 {best.symbol} {direction_label(best.direction)} | 分数 {best.score:.2f}")
    return f"opened {best.symbol} {best.direction}"


def status_payload(
    balance: float,
    positions: list[dict[str, Any]],
    strategy: dict[str, Any],
    candidates: list[Candidate],
    changes: list[str],
    events: list[str],
) -> dict[str, Any]:
    top = candidates[0] if candidates else None
    total_unrealized = sum(to_float(position.get("unrealizedProfit")) for position in positions)
    equity = balance + total_unrealized
    open_positions = [
        {
            "symbol": position["symbol"],
            "direction": position["direction"],
            "amount": round(to_float(position.get("amount")), 8),
            "entryPrice": round(to_float(position.get("entryPrice")), 8),
            "markPrice": round(to_float(position.get("markPrice")), 8),
            "unrealizedProfit": round(to_float(position.get("unrealizedProfit")), 8),
            "leverage": int(to_float(position.get("leverage"), strategy["leverage"])),
        }
        for position in positions
    ]
    return {
        "last_run": now_str(),
        "balance": round(balance, 4),
        "equity": round(equity, 4),
        "unrealized_pnl": round(total_unrealized, 4),
        "positions": len(positions),
        "open_positions": open_positions,
        "mode": strategy["mode"],
        "watchlist": strategy["coins"],
        "top_signal": {
            "symbol": top.symbol if top else None,
            "direction": top.direction if top else None,
            "score": round(top.score, 4) if top else None,
        },
        "strategy_changes": changes,
        "events": events,
    }


def optimize_only(strategy: dict[str, Any], changes: list[str]) -> int:
    print("=== Live strategy optimize ===")
    print(json.dumps(strategy, ensure_ascii=False, indent=2))
    if changes:
        print("changes:")
        for change in changes:
            print(f"- {change}")
    else:
        print("changes: none")
    return 0


def run_engine(mode: str, dry_run: bool = False) -> int:
    ensure_credentials()
    logger = EventLogger()
    client = BinanceClient(API_KEY, SECRET_KEY, dry_run=dry_run)
    strategy = load_strategy()
    trades = load_trades()

    logger.thinking(f"🔄 === 高频扫描 {datetime.now().strftime('%H:%M:%S')} ===")

    balance = client.get_balance()
    logger.thinking(f"💰 当前账户余额: <b>{balance:.4f} USDT</b>")

    strategy["mode"] = str(strategy.get("mode") or "profit-max-liquid")

    changes: list[str] = []
    if mode in {"run", "optimize", "close-only"}:
        strategy, changes = adapt_strategy(strategy, trades, logger)
        # 保存用户设置的 coins
        user_coins = strategy.get("coins", [])
        strategy["coins"] = select_watchlist(strategy, client)
        # 恢复用户设置的 coins
        if not strategy.get("dynamicCoinSelection", True):
            strategy["coins"] = user_coins
        write_json(STRATEGY_FILE, strategy)

    if mode == "optimize":
        logger.status(status_payload(balance, client.get_positions(), strategy, [], changes, ["optimize only"]))
        return optimize_only(strategy, changes)

    candidates = build_candidates(strategy, client, logger)
    positions = client.get_positions()
    events: list[str] = []

    if positions:
        logger.thinking(f"📊 当前持仓 {len(positions)} 个，优先处理离场")
        positions, close_events = manage_positions(client, logger, strategy, positions, trades, candidates)
        events.extend(close_events)
    else:
        logger.thinking("📭 当前空仓，进入高频信号筛选")

    if mode == "run":
        trades = load_trades()
        positions = client.get_positions()
        open_event = enter_trade(client, logger, strategy, trades, positions, candidates)
        events.append(open_event)

    balance = client.get_balance()
    logger.status(status_payload(balance, client.get_positions(), strategy, candidates, changes, events))
    logger.thinking("=== 扫描结束，等待下一轮 ===")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenClaw live strategy engine")
    parser.add_argument(
        "mode",
        nargs="?",
        default="run",
        choices=["run", "close-only", "optimize"],
        help="engine mode",
    )
    parser.add_argument("--dry-run", action="store_true", help="do not place real orders")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    dry_run = args.dry_run or os.getenv("TRADE_DRY_RUN") == "1"
    return run_engine(args.mode, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
