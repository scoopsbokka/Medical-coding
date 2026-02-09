#!/usr/bin/env python3
"""
Ignition & Fuel Crypto Screener

A fast, rate-limit-aware Binance screener that surfaces high-probability
momentum continuations. Designed for 24/7 operation with rich terminal output.

DISCLAIMER
- Educational use only. No warranty of any kind.
- You are responsible for complying with all exchange Terms of Service.
- This license applies to the code only; market data rights remain with providers.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
import numpy as np
import pandas as pd
import orjson
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from tenacity import retry, stop_after_attempt, wait_random_exponential, retry_if_exception_type

API_URLS = {
    "binance_spot": "https://api.binance.com/api/v3",
    "binance_futures": "https://fapi.binance.com/fapi/v1",
}

DEFAULT_RATE_LIMIT_PER_MIN = 1200
DEFAULT_WEIGHT_LIMIT = 1200

QUOTE_ASSET = "USDT"
LOOKBACK_BARS = 200

console = Console()


class RateLimitError(RuntimeError):
    def __init__(self, retry_after: float, message: str = "Rate limited") -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AsyncTokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self, amount: float = 1.0) -> None:
        async with self.lock:
            await self._refill()
            while self.tokens < amount:
                await asyncio.sleep(0.05)
                await self._refill()
            self.tokens -= amount

    async def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now


@dataclass
class SymbolSnapshot:
    symbol: str
    spot_1m: pd.DataFrame
    spot_5m: pd.DataFrame
    spot_15m: pd.DataFrame
    spot_4h: pd.DataFrame
    perp_5m: pd.DataFrame
    oi_history: pd.DataFrame
    funding_history: pd.DataFrame


@dataclass
class ScreenerResult:
    timestamp: datetime
    symbol: str
    ignition_score: float
    path: str
    reason_codes: List[str]
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    oi_delta_5m: float
    fr_drift: float
    spot_cvd_5m: float
    fut_cvd_5m: float
    vol_persist_15m: float
    perp_spot_vol_ratio: float
    vwap_state: str
    atr_1m: float
    bb4h_dist: float


class BinanceClient:
    def __init__(self, session: aiohttp.ClientSession, limiter: AsyncTokenBucket) -> None:
        self.session = session
        self.limiter = limiter
        self.spot_base = API_URLS["binance_spot"]
        self.futures_base = API_URLS["binance_futures"]

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_random_exponential(multiplier=1, max=20),
        retry=retry_if_exception_type((RateLimitError, aiohttp.ClientError, asyncio.TimeoutError)),
    )
    async def fetch_json(self, url: str, params: Optional[Dict[str, Any]] = None, weight: int = 1) -> Any:
        await self.limiter.acquire(weight)
        async with self.session.get(url, params=params, timeout=15) as response:
            if response.status in {418, 429}:
                retry_after = float(response.headers.get("Retry-After", "60"))
                raise RateLimitError(retry_after)
            response.raise_for_status()
            return await response.json(loads=orjson.loads)

    async def discover_symbols(self) -> List[str]:
        spot_info = await self.fetch_json(f"{self.spot_base}/exchangeInfo", weight=10)
        futures_info = await self.fetch_json(f"{self.futures_base}/exchangeInfo", weight=10)
        spot_symbols = {
            s["symbol"]
            for s in spot_info["symbols"]
            if s["status"] == "TRADING" and s["quoteAsset"] == QUOTE_ASSET
        }
        perp_symbols = {
            s["symbol"]
            for s in futures_info["symbols"]
            if s["status"] == "TRADING"
            and s["contractType"] == "PERPETUAL"
            and s["quoteAsset"] == QUOTE_ASSET
        }
        return sorted(spot_symbols.intersection(perp_symbols))

    async def top_symbols_by_volume(self, limit: int) -> List[str]:
        tickers = await self.fetch_json(f"{self.spot_base}/ticker/24hr", weight=40)
        filtered = [t for t in tickers if t.get("symbol", "").endswith(QUOTE_ASSET)]
        filtered.sort(key=lambda t: float(t.get("quoteVolume", 0.0)), reverse=True)
        return [t["symbol"] for t in filtered[:limit]]

    async def fetch_klines(self, symbol: str, interval: str, limit: int, market: str) -> pd.DataFrame:
        base = self.futures_base if market == "perp" else self.spot_base
        endpoint = f"{base}/klines"
        data = await self.fetch_json(endpoint, params={"symbol": symbol, "interval": interval, "limit": limit})
        df = pd.DataFrame(
            data,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trades",
                "taker_buy_base_volume",
                "taker_buy_quote_volume",
                "ignore",
            ],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        numeric_cols = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base_volume"]
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
        return df.set_index("timestamp")

    async def fetch_oi_history(self, symbol: str) -> pd.DataFrame:
        endpoint = f"{self.futures_base}/../futures/data/openInterestHist"
        data = await self.fetch_json(endpoint, params={"symbol": symbol, "period": "5m", "limit": LOOKBACK_BARS})
        df = pd.DataFrame(data)
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df["sumOpenInterestValue"] = pd.to_numeric(df["sumOpenInterestValue"], errors="coerce")
        return df.set_index("timestamp")

    async def fetch_funding_history(self, symbol: str) -> pd.DataFrame:
        endpoint = f"{self.futures_base}/fundingRate"
        data = await self.fetch_json(endpoint, params={"symbol": symbol, "limit": 100})
        df = pd.DataFrame(data)
        if df.empty:
            return df
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms")
        df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
        return df.set_index("fundingTime")


class FeatureBuilder:
    def __init__(self) -> None:
        pass

    def build(self, snapshot: SymbolSnapshot) -> Optional[Dict[str, Any]]:
        if snapshot.spot_5m.empty or snapshot.perp_5m.empty or snapshot.spot_4h.empty:
            return None

        vol_5m = snapshot.spot_5m["volume"]
        if len(vol_5m) < 22:
            return None

        vol_spike = vol_5m.iloc[-1] / vol_5m.iloc[-21:-1].mean()
        vol_15m = snapshot.spot_15m["volume"]
        vol_persist_15m = vol_15m.iloc[-1] / vol_15m.iloc[-5:-1].mean() if len(vol_15m) > 5 else 0.0

        spot_cvd_5m = self._cvd(snapshot.spot_5m).iloc[-1]
        fut_cvd_5m = self._cvd(snapshot.perp_5m).iloc[-1]

        oi_delta_5m = 0.0
        if not snapshot.oi_history.empty and len(snapshot.oi_history) > 1:
            oi = snapshot.oi_history["sumOpenInterestValue"]
            oi_delta_5m = (oi.iloc[-1] - oi.iloc[-2]) / max(oi.iloc[-2], 1e-9)

        fr_drift = 0.0
        if not snapshot.funding_history.empty and len(snapshot.funding_history) > 1:
            fr = snapshot.funding_history["fundingRate"]
            fr_drift = fr.iloc[-1] - fr.iloc[-2]

        perp_spot_vol_ratio = snapshot.perp_5m["quote_volume"].iloc[-1] / max(
            snapshot.spot_5m["quote_volume"].iloc[-1], 1e-9
        )

        atr_1m = self._atr(snapshot.spot_1m, 14)
        bb_upper, bb_mid, bb_lower = self._bbands(snapshot.spot_4h, 20, 2)
        bb4h_dist = (
            (snapshot.spot_4h["close"].iloc[-1] - bb_lower) / max(bb_upper - bb_lower, 1e-9)
            if bb_upper != bb_lower
            else 0.5
        )
        vwap = self._vwap(snapshot.spot_5m)
        vwap_state = "above" if snapshot.spot_5m["close"].iloc[-1] > vwap else "below"

        return {
            "vol_spike": vol_spike,
            "vol_persist_15m": vol_persist_15m,
            "spot_cvd_5m": spot_cvd_5m,
            "fut_cvd_5m": fut_cvd_5m,
            "oi_delta_5m": oi_delta_5m,
            "fr_drift": fr_drift,
            "perp_spot_vol_ratio": perp_spot_vol_ratio,
            "atr_1m": atr_1m,
            "bb4h_dist": bb4h_dist,
            "vwap_state": vwap_state,
            "latest_close": snapshot.spot_5m["close"].iloc[-1],
            "klines_4h": snapshot.spot_4h,
        }

    @staticmethod
    def _cvd(df: pd.DataFrame) -> pd.Series:
        taker_buy = df["taker_buy_base_volume"]
        taker_sell = df["volume"] - taker_buy
        return taker_buy - taker_sell

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> float:
        if df.empty or len(df) < period:
            return 0.0
        high_low = df["high"] - df["low"]
        high_close = np.abs(df["high"] - df["close"].shift())
        low_close = np.abs(df["low"] - df["close"].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return float(tr.rolling(window=period).mean().iloc[-1])

    @staticmethod
    def _bbands(df: pd.DataFrame, period: int, std_dev: float) -> Tuple[float, float, float]:
        if df.empty or len(df) < period:
            return 0.0, 0.0, 0.0
        sma = df["close"].rolling(window=period).mean().iloc[-1]
        std = df["close"].rolling(window=period).std().iloc[-1]
        upper = sma + std * std_dev
        lower = sma - std * std_dev
        return float(upper), float(sma), float(lower)

    @staticmethod
    def _vwap(df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        q = df["quote_volume"]
        p = (df["high"] + df["low"] + df["close"]) / 3
        return float((p * q).sum() / max(q.sum(), 1e-9))


class Scorer:
    def score(self, features: Dict[str, Dict[str, Any]]) -> List[ScreenerResult]:
        results: List[ScreenerResult] = []
        for symbol, feat in features.items():
            if not feat:
                continue
            score, path, reasons = self._ignite(feat)
            if score <= 0:
                continue
            levels = self._levels(feat)
            results.append(
                ScreenerResult(
                    timestamp=datetime.now(timezone.utc),
                    symbol=symbol,
                    ignition_score=score,
                    path=path,
                    reason_codes=reasons,
                    entry=levels["entry"],
                    stop=levels["stop"],
                    tp1=levels["tp1"],
                    tp2=levels["tp2"],
                    tp3=levels["tp3"],
                    oi_delta_5m=feat["oi_delta_5m"],
                    fr_drift=feat["fr_drift"],
                    spot_cvd_5m=feat["spot_cvd_5m"],
                    fut_cvd_5m=feat["fut_cvd_5m"],
                    vol_persist_15m=feat["vol_persist_15m"],
                    perp_spot_vol_ratio=feat["perp_spot_vol_ratio"],
                    vwap_state=feat["vwap_state"],
                    atr_1m=feat["atr_1m"],
                    bb4h_dist=feat["bb4h_dist"],
                )
            )
        return sorted(results, key=lambda r: r.ignition_score, reverse=True)

    def _ignite(self, feat: Dict[str, Any]) -> Tuple[float, str, List[str]]:
        reasons: List[str] = []

        if feat["bb4h_dist"] > 0.95:
            return 0.0, "veto", ["overextended_bb4h"]

        spot_led_score = 0.0
        if (
            feat["vol_spike"] > 2.5
            and feat["spot_cvd_5m"] > 0
            and feat["perp_spot_vol_ratio"] < 1.5
            and feat["vwap_state"] == "above"
        ):
            spot_led_score += min(feat["vol_spike"] * 10, 50)
            spot_led_score += min(feat["vol_persist_15m"] * 10, 20)
            spot_led_score += min(feat["spot_cvd_5m"] / 1000, 1) * 15
            spot_led_score += max(0.0, 1 - feat["perp_spot_vol_ratio"]) * 15
            reasons.append("spot_led_volume_spike")

        perp_led_score = 0.0
        if feat["oi_delta_5m"] > 0.02 and feat["fut_cvd_5m"] > 0 and feat["spot_cvd_5m"] > -100:
            perp_led_score += min(feat["oi_delta_5m"] * 1000, 40)
            perp_led_score += min(feat["fut_cvd_5m"] / 1000, 1) * 30
            perp_led_score += 10 if feat["fr_drift"] > 0 else 0
            reasons.append("perp_led_oi_surge")

        if spot_led_score >= perp_led_score and spot_led_score > 0:
            return min(spot_led_score, 100), "spot-led", reasons
        if perp_led_score > 0:
            return min(perp_led_score, 100), "perp-led", reasons
        return 0.0, "none", []

    @staticmethod
    def _levels(feat: Dict[str, Any]) -> Dict[str, float]:
        entry = feat["latest_close"]
        stop = entry - (feat["atr_1m"] * 2)
        klines_4h = feat["klines_4h"]
        tp1 = klines_4h["high"].rolling(24).max().iloc[-1]
        prev_day = klines_4h.iloc[-7:-1]
        pivot = (prev_day["high"].max() + prev_day["low"].min() + prev_day["close"].iloc[-1]) / 3
        r1 = (2 * pivot) - prev_day["low"].min()
        tp2 = r1
        tp3 = pivot + (prev_day["high"].max() - prev_day["low"].min())
        return {"entry": float(entry), "stop": float(stop), "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3)}


class ScreenerApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        rate_per_sec = DEFAULT_RATE_LIMIT_PER_MIN / 60
        self.limiter = AsyncTokenBucket(rate_per_sec, DEFAULT_WEIGHT_LIMIT)
        self.feature_builder = FeatureBuilder()
        self.scorer = Scorer()
        self.semaphore = asyncio.Semaphore(args.max_concurrency)

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            client = BinanceClient(session, self.limiter)
            symbols = await self._resolve_symbols(client)
            if not symbols:
                console.print("[red]No symbols available. Exiting.[/red]")
                return

            with Live(console=console, refresh_per_second=2) as live:
                cycles_done = 0
                while True:
                    start = time.time()
                    snapshots = await self._fetch_snapshots(client, symbols)
                    features = self._build_features(snapshots)
                    results = self.scorer.score(features)
                    self._render(live, results, len(symbols))
                    if self.args.save_csv:
                        self._save_csv(results)
                    cycles_done += 1
                    if self.args.cycles and cycles_done >= self.args.cycles:
                        break
                    elapsed = time.time() - start
                    await asyncio.sleep(max(0, self.args.scan_interval - elapsed))

    async def _resolve_symbols(self, client: BinanceClient) -> List[str]:
        if self.args.symbols == "all":
            return await client.discover_symbols()
        if self.args.symbols == "top":
            top = await client.top_symbols_by_volume(self.args.top_n)
            available = set(await client.discover_symbols())
            return [symbol for symbol in top if symbol in available]
        return [s.strip().upper() for s in self.args.symbols.split(",") if s.strip()]

    async def _fetch_snapshots(self, client: BinanceClient, symbols: List[str]) -> Dict[str, SymbolSnapshot]:
        tasks = [self._fetch_symbol(client, symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        snapshots: Dict[str, SymbolSnapshot] = {}
        for result in results:
            if isinstance(result, SymbolSnapshot):
                snapshots[result.symbol] = result
        return snapshots

    async def _fetch_symbol(self, client: BinanceClient, symbol: str) -> Optional[SymbolSnapshot]:
        async with self.semaphore:
            try:
                spot_1m, spot_5m, spot_15m, spot_4h, perp_5m, oi_hist, fr_hist = await asyncio.gather(
                    client.fetch_klines(symbol, "1m", LOOKBACK_BARS, "spot"),
                    client.fetch_klines(symbol, "5m", LOOKBACK_BARS, "spot"),
                    client.fetch_klines(symbol, "15m", LOOKBACK_BARS, "spot"),
                    client.fetch_klines(symbol, "4h", LOOKBACK_BARS, "spot"),
                    client.fetch_klines(symbol, "5m", LOOKBACK_BARS, "perp"),
                    client.fetch_oi_history(symbol),
                    client.fetch_funding_history(symbol),
                )
                return SymbolSnapshot(
                    symbol=symbol,
                    spot_1m=spot_1m,
                    spot_5m=spot_5m,
                    spot_15m=spot_15m,
                    spot_4h=spot_4h,
                    perp_5m=perp_5m,
                    oi_history=oi_hist,
                    funding_history=fr_hist,
                )
            except RateLimitError as exc:
                await asyncio.sleep(exc.retry_after)
            except Exception as exc:  # noqa: BLE001 - log and move on
                logging.warning("Failed to fetch %s: %s", symbol, exc)
            return None

    def _build_features(self, snapshots: Dict[str, SymbolSnapshot]) -> Dict[str, Dict[str, Any]]:
        features: Dict[str, Dict[str, Any]] = {}
        for symbol, snapshot in snapshots.items():
            feat = self.feature_builder.build(snapshot)
            if feat:
                features[symbol] = feat
        return features

    def _render(self, live: Live, results: List[ScreenerResult], symbol_count: int) -> None:
        table = Table(
            title=f"Ignition Screener • {datetime.now(timezone.utc).isoformat()} • Symbols {symbol_count}",
            show_lines=False,
        )
        table.add_column("Symbol", style="cyan", no_wrap=True)
        table.add_column("Score", style="magenta")
        table.add_column("Path", style="green")
        table.add_column("Entry", justify="right")
        table.add_column("Stop", justify="right")
        table.add_column("TP1", justify="right")
        table.add_column("Reasons", style="yellow")

        for result in results[: self.args.top_k]:
            score_style = "bold green" if result.ignition_score >= 80 else "bold yellow"
            score_text = Text(f"{result.ignition_score:.1f}", style=score_style)
            table.add_row(
                result.symbol,
                score_text,
                result.path,
                f"{result.entry:.4f}",
                f"{result.stop:.4f}",
                f"{result.tp1:.4f}",
                ", ".join(result.reason_codes) or "-",
            )

        live.update(table, refresh=True)

    def _save_csv(self, results: List[ScreenerResult]) -> None:
        if not results:
            return
        filename = self.args.csv_path
        with open(filename, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "timestamp",
                    "symbol",
                    "score",
                    "path",
                    "entry",
                    "stop",
                    "tp1",
                    "tp2",
                    "tp3",
                    "oi_delta_5m",
                    "fr_drift",
                    "spot_cvd_5m",
                    "fut_cvd_5m",
                    "vol_persist_15m",
                    "perp_spot_vol_ratio",
                    "vwap_state",
                    "atr_1m",
                    "bb4h_dist",
                    "reason_codes",
                ]
            )
            for r in results:
                writer.writerow(
                    [
                        r.timestamp.isoformat(),
                        r.symbol,
                        f"{r.ignition_score:.2f}",
                        r.path,
                        f"{r.entry:.4f}",
                        f"{r.stop:.4f}",
                        f"{r.tp1:.4f}",
                        f"{r.tp2:.4f}",
                        f"{r.tp3:.4f}",
                        f"{r.oi_delta_5m:.6f}",
                        f"{r.fr_drift:.6f}",
                        f"{r.spot_cvd_5m:.2f}",
                        f"{r.fut_cvd_5m:.2f}",
                        f"{r.vol_persist_15m:.2f}",
                        f"{r.perp_spot_vol_ratio:.2f}",
                        r.vwap_state,
                        f"{r.atr_1m:.4f}",
                        f"{r.bb4h_dist:.2f}",
                        "|".join(r.reason_codes),
                    ]
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ignition & Fuel Crypto Screener")
    parser.add_argument("--symbols", default="top", help="all | top | comma-separated list")
    parser.add_argument("--top-n", type=int, default=50, help="Top N symbols by volume")
    parser.add_argument("--top-k", type=int, default=10, help="Top K rows to display")
    parser.add_argument("--scan-interval", type=int, default=300, help="Scan interval (seconds)")
    parser.add_argument("--cycles", type=int, default=0, help="Number of cycles (0 = infinite)")
    parser.add_argument("--max-concurrency", type=int, default=6, help="Max concurrent symbol fetches")
    parser.add_argument("--save-csv", action="store_true", help="Write CSV output")
    parser.add_argument("--csv-path", default="ignition_results.csv", help="CSV output path")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def main() -> None:
    configure_logging()
    args = parse_args()
    app = ScreenerApp(args)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        console.print("\n[red]Shutdown requested by user.[/red]")


if __name__ == "__main__":
    main()
