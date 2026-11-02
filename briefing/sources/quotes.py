"""Delayed stock quotes behind one small interface.

Providers are tried in order (QUOTE_PROVIDERS). Finnhub is the recommended source once you
have a free key. Cboe's delayed-quote JSON and yfinance work without a key but are unofficial
and can disappear or rate-limit without notice, so treat them as demo-grade and swap in a
licensed feed for production. Every quote carries its source and an as-of time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

import httpx

from ..cache import Cache
from ..models import Quote

log = logging.getLogger(__name__)
UA = "stock-briefing/0.1 (local demo)"


class QuoteError(RuntimeError):
    pass


class QuoteProvider(Protocol):
    name: str

    def get(self, symbol: str) -> Quote: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(x) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


class CboeProvider:
    name = "cboe"
    URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{symbol}.json"

    def __init__(self, http: httpx.Client | None = None):
        self._http = http or httpx.Client(headers={"User-Agent": UA}, timeout=10, follow_redirects=True)

    def get(self, symbol: str) -> Quote:
        resp = self._http.get(self.URL.format(symbol=symbol.replace("-", ".")))
        if resp.status_code != 200:
            raise QuoteError(f"cboe HTTP {resp.status_code}")
        d = resp.json().get("data") or {}
        price = _num(d.get("current_price"))
        if not price:
            raise QuoteError("cboe returned no price")
        t = d.get("last_trade_time")
        return Quote(
            symbol=symbol,
            price=price,
            change=_num(d.get("price_change")),
            change_pct=_num(d.get("price_change_percent")),
            previous_close=_num(d.get("prev_day_close")),
            open=_num(d.get("open")),
            day_high=_num(d.get("high")),
            day_low=_num(d.get("low")),
            volume=int(d["volume"]) if d.get("volume") is not None else None,
            as_of=f"{t} ET" if t else _now(),
            source="Cboe delayed quotes (unofficial)",
        )


class FinnhubProvider:
    name = "finnhub"
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.api_key = api_key
        self._http = http or httpx.Client(headers={"User-Agent": UA}, timeout=10)

    def _call(self, path: str, **params):
        resp = self._http.get(f"{self.BASE}/{path}", params={**params, "token": self.api_key})
        if resp.status_code != 200:
            raise QuoteError(f"finnhub {path} HTTP {resp.status_code}")
        return resp.json()

    def get(self, symbol: str) -> Quote:
        q = self._call("quote", symbol=symbol)
        if not q.get("c"):
            raise QuoteError("finnhub returned no price")
        cap = hi = lo = None
        try:  # best-effort enrichment; the quote itself already succeeded
            cap = _num(self._call("stock/profile2", symbol=symbol).get("marketCapitalization"))
            m = self._call("stock/metric", symbol=symbol, metric="all").get("metric", {})
            hi, lo = _num(m.get("52WeekHigh")), _num(m.get("52WeekLow"))
        except Exception as exc:  # noqa: BLE001
            log.info("finnhub enrichment skipped: %s", exc)
        ts = q.get("t")
        return Quote(
            symbol=symbol,
            price=float(q["c"]),
            change=_num(q.get("d")),
            change_pct=_num(q.get("dp")),
            previous_close=_num(q.get("pc")),
            open=_num(q.get("o")),
            day_high=_num(q.get("h")),
            day_low=_num(q.get("l")),
            year_high=hi,
            year_low=lo,
            market_cap=cap * 1_000_000 if cap else None,  # finnhub reports millions
            as_of=datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else _now(),
            source="Finnhub",
        )


class YFinanceProvider:
    name = "yfinance"

    def get(self, symbol: str) -> Quote:
        import yfinance as yf  # imported lazily: it is slow to import and optional at runtime

        fi = yf.Ticker(symbol).fast_info
        price = _num(fi.get("last_price"))
        if not price:
            raise QuoteError("yfinance returned no price (likely rate-limited)")
        prev = _num(fi.get("previous_close"))
        return Quote(
            symbol=symbol,
            price=price,
            change=round(price - prev, 4) if prev else None,
            change_pct=round((price - prev) / prev * 100, 4) if prev else None,
            previous_close=prev,
            day_high=_num(fi.get("day_high")),
            day_low=_num(fi.get("day_low")),
            year_high=_num(fi.get("year_high")),
            year_low=_num(fi.get("year_low")),
            volume=int(fi["last_volume"]) if fi.get("last_volume") else None,
            market_cap=_num(fi.get("market_cap")),
            currency=fi.get("currency") or "USD",
            as_of=_now(),
            source="Yahoo Finance via yfinance (unofficial)",
        )


class QuoteService:
    def __init__(self, providers: list[QuoteProvider], cache: Cache, ttl: int = 60):
        self.providers = providers
        self.cache = cache
        self.ttl = ttl

    def get(self, symbol: str) -> Quote:
        key = f"quote:{symbol}"
        hit = self.cache.get_json(key)
        if hit:
            return Quote(**hit)
        errors: list[str] = []
        for p in self.providers:
            try:
                q = p.get(symbol)
            except Exception as exc:  # noqa: BLE001 - provider failures are expected; try the next
                errors.append(f"{p.name}: {exc}")
                continue
            self.cache.set_json(key, q.to_dict(), ttl=self.ttl)
            return q
        raise QuoteError("No quote provider succeeded (" + "; ".join(errors) + ")")


def build_quote_service(provider_names: str, finnhub_key: str, cache: Cache, ttl: int) -> QuoteService:
    providers: list[QuoteProvider] = []
    for name in (n.strip() for n in provider_names.split(",") if n.strip()):
        if name == "finnhub":
            if finnhub_key:
                providers.append(FinnhubProvider(finnhub_key))
        elif name == "cboe":
            providers.append(CboeProvider())
        elif name == "yfinance":
            providers.append(YFinanceProvider())
        else:
            raise ValueError(f"Unknown quote provider '{name}'")
    return QuoteService(providers, cache, ttl)
