#!/usr/bin/env python3
import argparse
import asyncio
import logging
import os
import random
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, Optional, Tuple

import aiohttp
import orjson
import websockets
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

# ##############################################################################
# Ignition Screener - Fast, Rate-Limit Safe, Rich Console Output
# ##############################################################################

BINANCE_SPOT_REST = "https://api.binance.com/api/v3"
BINANCE_FUTURES_REST = "https://fapi.binance.com/fapi/v1"
BINANCE_WS = "wss://stream.binance.com:9443/stream"

DEFAULT_INTERVAL = "5m"
DEFAULT_TOP_N = 50
DEFAULT_SCAN_SECONDS = 3
DEFAULT_MIN_USD_VOL = 5_000_000
DEFAULT_ROLLING = 48
DEFAULT_SCORE_THRESHOLD = 70

console = Console()
log = logging.getLogger("ignition")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TokenBucket:
    capacity: int
    refill_rate_per_s: float
    tokens: float = field(default=0)
    updated_at: float = field(default_factory=time.monotonic)

    async def acquire(self, amount: float = 1.0) -> None:
        while True:
            self._refill()
            if self.tokens >= amount:
                self.tokens -= amount
                return
            await asyncio.sleep(self._sleep_time(amount))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate_per_s)
        self.updated_at = now

    def _sleep_time(self, amount: float) -> float:
        missing = max(0.0, amount - self.tokens)
        if self.refill_rate_per_s <= 0:
            return 1.0
        return missing / self.refill_rate_per_s


@dataclass
class RateLimitState:
    weight_used_1m: int = 0
    last_updated: float = field(default_factory=time.monotonic)

    def update_from_headers(self, headers: aiohttp.typedefs.LooseHeaders) -> None:
        used = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        if used is not None:
            try:
                self.weight_used_1m = int(used)
                self.last_updated = time.monotonic()
            except ValueError:
                pass


@dataclass
class KlineState:
    symbol: str
    closes: Deque[float] = field(default_factory=deque)
    volumes: Deque[float] = field(default_factory=deque)
    returns: Deque[float] = field(default_factory=deque)
    last_close: Optional[float] = None
    last_update: Optional[datetime] = None

    def update(self, close_price: float, volume: float, maxlen: int) -> None:
        if self.last_close is not None:
            ret = (close_price - self.last_close) / self.last_close
            self.returns.append(ret)
            if len(self.returns) > maxlen:
                self.returns.popleft()
        self.closes.append(close_price)
        if len(self.closes) > maxlen:
            self.closes.popleft()
        self.volumes.append(volume)
        if len(self.volumes) > maxlen:
            self.volumes.popleft()
        self.last_close = close_price
        self.last_update = utc_now()


@dataclass
class Signal:
    symbol: str
    score: float
    price: float
    change_pct: float
    vol_multiplier: float
    oi_change: Optional[float]
    timestamp: datetime


class BinanceClient:
    def __init__(self, session: aiohttp.ClientSession, limiter: TokenBucket, rl_state: RateLimitState):
        self.session = session
        self.limiter = limiter
        self.rl_state = rl_state

    async def _get_json(self, url: str, params: Optional[dict] = None, weight: int = 1) -> dict:
        await self.limiter.acquire(weight)
        async with self.session.get(url, params=params, timeout=10) as resp:
            self.rl_state.update_from_headers(resp.headers)
            if resp.status in {418, 429}:
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after else 5 + random.random() * 5
                log.warning("Rate limit triggered. Sleeping for %.1fs", sleep_for)
                await asyncio.sleep(sleep_for)
                raise RuntimeError(f"Rate limited: {resp.status}")
            resp.raise_for_status()
            return await resp.json(loads=orjson.loads)

    async def fetch_usdt_symbols(self) -> List[str]:
        data = await self._get_json(f"{BINANCE_SPOT_REST}/exchangeInfo", weight=10)
        symbols = []
        for item in data.get("symbols", []):
            if item.get("status") == "TRADING" and item.get("quoteAsset") == "USDT":
                symbols.append(item.get("symbol"))
        return symbols

    async def fetch_top_volume_symbols(self, limit: int, min_usd_vol: float) -> List[str]:
        data = await self._get_json(f"{BINANCE_SPOT_REST}/ticker/24hr", weight=40)
        filtered = []
        for row in data:
            try:
                quote_vol = float(row.get("quoteVolume", 0))
            except ValueError:
                continue
            if quote_vol < min_usd_vol:
                continue
            symbol = row.get("symbol")
            if symbol and symbol.endswith("USDT"):
                filtered.append((symbol, quote_vol))
        filtered.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in filtered[:limit]]

    async def fetch_open_interest(self, symbol: str) -> Optional[float]:
        try:
            data = await self._get_json(
                f"{BINANCE_FUTURES_REST}/openInterest",
                params={"symbol": symbol},
                weight=1,
            )
            return float(data.get("openInterest"))
        except Exception:
            return None


def build_stream_url(symbols: Iterable[str], interval: str) -> str:
    streams = "/".join(f"{s.lower()}@kline_{interval}" for s in symbols)
    return f"{BINANCE_WS}?streams={streams}"


def score_symbol(state: KlineState, rolling: int, oi_change: Optional[float]) -> Optional[Signal]:
    if len(state.volumes) < max(10, rolling // 4):
        return None
    latest_close = state.closes[-1]
    latest_volume = state.volumes[-1]
    avg_volume = sum(state.volumes) / len(state.volumes)
    vol_multiplier = latest_volume / avg_volume if avg_volume else 0
    change_pct = 0.0
    if len(state.closes) > 1:
        change_pct = (state.closes[-1] - state.closes[-2]) / state.closes[-2] * 100

    momentum = sum(state.returns) / len(state.returns) if state.returns else 0
    trend_score = max(0.0, min(1.0, momentum * 100))

    score = 0.0
    score += min(40.0, vol_multiplier * 15)
    score += min(30.0, abs(change_pct) * 2)
    score += min(20.0, trend_score)
    if oi_change is not None:
        score += min(10.0, oi_change)

    if score < DEFAULT_SCORE_THRESHOLD:
        return None

    return Signal(
        symbol=state.symbol,
        score=score,
        price=latest_close,
        change_pct=change_pct,
        vol_multiplier=vol_multiplier,
        oi_change=oi_change,
        timestamp=utc_now(),
    )


def build_table(signals: List[Signal], rl_state: RateLimitState) -> Table:
    table = Table(title="Ignition Screener", expand=True)
    table.add_column("Symbol", style="bold cyan")
    table.add_column("Score", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("5m %", justify="right")
    table.add_column("Vol x", justify="right")
    table.add_column("OI", justify="right")
    table.add_column("Updated", justify="right")

    for signal in signals:
        score_style = "green" if signal.score >= 85 else "yellow" if signal.score >= 75 else "magenta"
        oi_text = "-" if signal.oi_change is None else f"{signal.oi_change:.2f}%"
        table.add_row(
            signal.symbol,
            Text(f"{signal.score:.1f}", style=score_style),
            f"{signal.price:.6f}",
            f"{signal.change_pct:.2f}%",
            f"{signal.vol_multiplier:.2f}",
            oi_text,
            signal.timestamp.strftime("%H:%M:%S"),
        )

    footer = Text(
        f"Binance weight (1m): {rl_state.weight_used_1m} | signals: {len(signals)}",
        style="bold blue",
    )
    table.caption = footer
    return table


def compute_oi_delta(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old * 100


async def stream_klines(
    symbols: List[str],
    interval: str,
    states: Dict[str, KlineState],
    rolling: int,
    scan_seconds: int,
    client: BinanceClient,
) -> None:
    url = build_stream_url(symbols, interval)
    backoff = 1.0
    oi_cache: Dict[str, float] = {}

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                log.info("Connected to Binance WS with %d symbols", len(symbols))
                backoff = 1.0
                last_scan = time.monotonic()
                while True:
                    raw = await ws.recv()
                    payload = orjson.loads(raw)
                    event = payload.get("data", {})
                    if event.get("e") != "kline":
                        continue
                    kline = event.get("k", {})
                    symbol = event.get("s")
                    if not symbol or symbol not in states:
                        continue
                    if not kline.get("x"):
                        continue
                    close_price = float(kline.get("c"))
                    volume = float(kline.get("v"))
                    states[symbol].update(close_price, volume, rolling)

                    now = time.monotonic()
                    if now - last_scan >= scan_seconds:
                        signals = []
                        for sym, state in states.items():
                            oi_delta = None
                            if sym.endswith("USDT"):
                                if sym not in oi_cache or now - last_scan > 30:
                                    oi_old = oi_cache.get(sym)
                                    oi_new = await client.fetch_open_interest(sym)
                                    oi_cache[sym] = oi_new if oi_new else oi_cache.get(sym)
                                    oi_delta = compute_oi_delta(oi_old, oi_new)
                            signal = score_symbol(state, rolling, oi_delta)
                            if signal:
                                signals.append(signal)
                        signals.sort(key=lambda s: s.score, reverse=True)
                        yield signals
                        last_scan = now
        except Exception as exc:
            log.warning("WebSocket error: %s. Reconnecting in %.1fs", exc, backoff)
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 30)


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING)
    bucket = TokenBucket(capacity=1200, refill_rate_per_s=1200 / 60)
    rl_state = RateLimitState()

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = BinanceClient(session, bucket, rl_state)

        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = await client.fetch_top_volume_symbols(args.top_n, args.min_usd_vol)

        states = {symbol: KlineState(symbol=symbol) for symbol in symbols}

        if not symbols:
            console.print("[red]No symbols available. Check API connectivity.[/red]")
            return 1

        console.print(
            f"[bold green]Tracking {len(symbols)} symbols[/bold green]"
            f" | interval {args.interval}"
            f" | rolling {args.rolling}"
        )

        signals_stream = stream_klines(
            symbols,
            args.interval,
            states,
            args.rolling,
            args.scan_seconds,
            client,
        )

        with Live(console=console, refresh_per_second=2) as live:
            async for signals in signals_stream:
                live.update(build_table(signals, rl_state))

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ignition Screener (Binance)")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--min-usd-vol", type=float, default=DEFAULT_MIN_USD_VOL)
    parser.add_argument("--rolling", type=int, default=DEFAULT_ROLLING)
    parser.add_argument("--scan-seconds", type=int, default=DEFAULT_SCAN_SECONDS)
    parser.add_argument("--symbols", type=str, default="")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def shutdown() -> None:
        stop_event.set()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: shutdown())

    try:
        return loop.run_until_complete(run(args))
    except KeyboardInterrupt:
        return 130
    finally:
        loop.stop()
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
