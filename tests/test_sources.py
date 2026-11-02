from datetime import date

import fakeredis
import httpx
import pytest

from briefing.cache import Cache
from briefing.models import Filing
from briefing.sources.edgar import EdgarClient, EdgarError
from briefing.sources.quotes import CboeProvider, QuoteError, QuoteService, build_quote_service

from .fakes import ACC_8, FakeEdgar, FakeQuotes


def f(form, filed, acc):
    return Filing(cik="1", accession=acc, form=form, filed=filed, report_date="", primary_doc="d.htm")


def test_sec_user_agent_is_mandatory_and_must_include_a_contact(tmp_path):
    for bad in ("", "just-a-name"):
        with pytest.raises(EdgarError, match="SEC_USER_AGENT"):
            EdgarClient(bad, Cache(None), tmp_path)


def test_resolve_and_discovery_use_real_client_logic_over_canned_http(tmp_path):
    e = FakeEdgar(Cache(None), tmp_path)
    c = e.resolve("acme")
    assert (c.ticker, c.cik, c.name) == ("ACME", "1234567", "Acme Corp")
    filings = e.select_filings(e.recent_filings(c.cik))
    # latest 10-K and latest 10-Q (older 10-K ignored) plus the recent 8-K, newest first
    assert [x.form for x in filings] == ["8-K", "10-Q", "10-K"]
    assert filings[0].items == ("2.02", "9.01")
    with pytest.raises(EdgarError, match="No SEC registrant"):
        e.resolve("NOPE")
    with pytest.raises(EdgarError, match="not a valid ticker"):
        e.resolve("../etc")


def test_select_filings_applies_8k_lookback_and_cap(tmp_path):
    e = EdgarClient("T t@example.com", Cache(None), tmp_path, lookback_days_8k=30, max_8k=2)
    today = date(2026, 11, 1)
    filings = [f("10-K", "2025-10-31", "k1"), f("10-K", "2024-10-31", "k0"), f("10-Q", "2026-07-31", "q1")]
    filings += [f("8-K", d, f"e{i}") for i, d in enumerate(["2026-10-25", "2026-10-20", "2026-10-15", "2026-08-01"])]
    picked = e.select_filings(filings, today=today)
    assert [x.accession for x in picked] == ["e0", "e1", "q1", "k1"]  # 3rd recent 8-K capped, Aug 8-K too old


def test_statement_reports_skip_parentheticals_and_exhibits_are_found(tmp_path):
    e = FakeEdgar(Cache(None), tmp_path)
    filing = next(x for x in e.select_filings(e.recent_filings("1234567")) if x.form == "10-K")
    assert [r[1] for r in e.statement_reports(filing)] == ["R2.htm", "R4.htm"]
    eightk = next(x for x in e.select_filings(e.recent_filings("1234567")) if x.accession == ACC_8)
    assert e.exhibit_99_files(eightk) == ["acme-ex991.htm"]


def test_documents_are_cached_on_disk_forever(tmp_path):
    e = FakeEdgar(Cache(None), tmp_path)
    url = "https://www.sec.gov/Archives/edgar/data/1234567/000123456725000010/acme-10k.htm"
    e.fetch_text(url)
    e.fetch_text(url)
    assert e.requests.count(url) == 1


def test_http_errors_map_to_clear_messages(tmp_path):
    def handler(request):
        return httpx.Response(403)

    e = EdgarClient("T t@example.com", Cache(None), tmp_path, min_interval=0, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EdgarError, match="403"):
        e.fetch_bytes("https://www.sec.gov/x.json")


def test_quote_service_falls_through_providers_and_caches():
    class Down:
        name = "down"

        def get(self, s):
            raise QuoteError("nope")

    ok = FakeQuotes()
    svc = QuoteService([Down(), ok], Cache(None), ttl=60)
    assert svc.get("AAPL").price == 123.45
    svc.get("AAPL")
    assert ok.calls == 1  # second call served from cache
    with pytest.raises(QuoteError, match="down: nope"):
        QuoteService([Down()], Cache(None), ttl=0).get("AAPL")


def test_cboe_mapping_and_finnhub_is_skipped_without_a_key():
    payload = {"data": {"current_price": 336.26, "price_change": -8.37, "price_change_percent": -2.4287, "prev_day_close": 344.63, "volume": 25310885, "last_trade_time": "2026-11-02T13:41:13", "open": 342.56, "high": 343.78, "low": 336.18}}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    q = CboeProvider(client).get("AAPL")
    assert (q.price, q.change, q.previous_close, q.volume) == (336.26, -8.37, 344.63, 25310885)
    assert q.as_of == "2026-11-02T13:41:13 ET" and q.delayed
    svc = build_quote_service("finnhub,cboe", "", Cache(None), 60)
    assert [p.name for p in svc.providers] == ["cboe"]


def test_cache_memory_ttl_and_redis_backends(monkeypatch):
    mem = Cache(None)
    mem.set_json("k", {"a": 1}, ttl=60)
    assert mem.get_json("k") == {"a": 1} and mem.backend == "memory"
    mem.set_json("old", 1, ttl=1)
    monkeypatch.setattr("briefing.cache.time.time", lambda: 10**12)
    assert mem.get_json("old") is None

    redis_cache = Cache.with_client(fakeredis.FakeRedis(decode_responses=True))
    redis_cache.set_json("k", [1, 2], ttl=60)
    assert redis_cache.get_json("k") == [1, 2] and redis_cache.backend == "redis"
    redis_cache.delete("k")
    assert redis_cache.get_json("k") is None


def test_unreachable_redis_falls_back_to_memory():
    c = Cache("redis://127.0.0.1:1/0")  # nothing listens here
    c.set_json("k", 1)
    assert c.get_json("k") == 1 and c.backend == "memory"


# ---- Gemini fallback chain (no network: fake chains) -----------------------------------------
from briefing.llm import DraftBrief, DraftCitation, DraftClaim, GeminiSummarizer, LLMUnavailable  # noqa: E402


class _Chain:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def invoke(self, messages, config=None):
        self.calls += 1
        o = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(o, Exception):
            raise o
        return o


GOOD = DraftBrief(claims=[DraftClaim(facet="business", text="x", citations=[DraftCitation(source_id="S1", quote="y")])])


def _summ(*chains, attempts=2):
    return GeminiSummarizer([n for n, _ in chains], "k", chains=list(chains), sleep=lambda s: None, attempts=attempts)


def _draft(s):
    return s.draft("Co", {"S1": {"source_type": "doc", "text": "t", "item": "i", "form": "10-K", "filed": "2025-01-01", "origin": "html", "id": "i", "ticker": "T", "accession": "a", "section_key": "k"}}, {"business": ["S1"]})


def test_overloaded_primary_is_retried_then_the_fallback_answers():
    a, b = _Chain(RuntimeError("503 UNAVAILABLE. high demand")), _Chain(GOOD)
    d = _draft(_summ(("primary", a), ("fallback", b)))
    assert (a.calls, b.calls, d._model_used) == (2, 1, "fallback")


def test_a_transient_blip_recovers_on_the_same_model():
    a = _Chain(RuntimeError("503 UNAVAILABLE"), GOOD)
    d = _draft(_summ(("primary", a), ("fallback", _Chain(GOOD))))
    assert (a.calls, d._model_used) == (2, "primary")


def test_quota_and_retired_models_skip_ahead_without_retrying():
    a, b, c = _Chain(RuntimeError("429 RESOURCE_EXHAUSTED quota")), _Chain(RuntimeError("404 NOT_FOUND")), _Chain(GOOD)
    d = _draft(_summ(("a", a), ("b", b), ("c", c)))
    assert (a.calls, b.calls, c.calls, d._model_used) == (1, 1, 1, "c")


def test_real_bugs_are_raised_not_hidden_behind_a_fallback():
    a, b = _Chain(ValueError("400 INVALID_ARGUMENT: bad schema")), _Chain(GOOD)
    with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
        _draft(_summ(("a", a), ("b", b)))
    assert b.calls == 0


def test_when_every_model_fails_the_error_names_them_all():
    with pytest.raises(LLMUnavailable, match=r"Gemini could not answer right now \(a: service overloaded \(503\); b: quota or rate limit exceeded \(429\)\)"):
        _draft(_summ(("a", _Chain(RuntimeError("503 UNAVAILABLE"))), ("b", _Chain(RuntimeError("429 quota")))))


def test_a_busy_service_gets_a_second_pass_but_quota_and_retired_models_do_not():
    a = _Chain(RuntimeError("503 UNAVAILABLE"), RuntimeError("503 UNAVAILABLE"), RuntimeError("503 UNAVAILABLE"), GOOD)
    d = _draft(_summ(("a", a), attempts=2))  # pass 1: two 503s. Pass 2: one more 503, then success on the 4th call
    assert d._model_used == "a" and a.calls == 4

    quota = _Chain(RuntimeError("429 RESOURCE_EXHAUSTED"))
    with pytest.raises(LLMUnavailable):
        _draft(_summ(("q", quota)))
    assert quota.calls == 1  # no second pass for something that will not clear in seconds


# ---- pacing, provider retry hints, readable errors ---------------------------------------------
from briefing.embeddings import Embedder, RateLimiter  # noqa: E402
from briefing.events import retry_delay, short_error  # noqa: E402


def test_rate_limiter_blocks_until_the_window_has_room(monkeypatch):
    clock = {"t": 0.0}
    slept: list[float] = []
    monkeypatch.setattr("briefing.embeddings.time.sleep", lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
    lim = RateLimiter(100, now=lambda: clock["t"])
    lim.acquire(60)
    lim.acquire(30)  # 90 used, no wait
    assert slept == []
    lim.acquire(30)  # would make 120 > 100: must wait for the first batch to age out of the minute
    assert len(slept) == 1 and 59.9 < slept[0] < 60.2
    assert RateLimiter(0).acquire(10**6) is None  # 0 = unlimited


def test_embedder_batches_are_small_enough_to_pace_smoothly():
    assert Embedder(object(), "m", 8, rpm=90).batch_size == 30
    assert Embedder(object(), "m", 8, rpm=0).batch_size == 64


def test_429_with_a_retry_hint_waits_as_asked_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("briefing.embeddings.time.sleep", sleeps.append)

    class Flaky:
        n = 0

        def embed_documents(self, texts):
            Flaky.n += 1
            if Flaky.n == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. {'message': 'quota. Please retry in 52.7s.', 'retryDelay': '52s'}")
            return [[0.0] * 4 for _ in texts]

        def embed_query(self, t):
            return [0.0] * 4

    assert len(Embedder(Flaky(), "m", 4).embed_documents(["a", "b"])) == 2
    assert sleeps == [pytest.approx(53.7)]  # the provider's 52.7s plus a second of margin


def test_provider_error_blobs_become_one_readable_line():
    blob = "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota ... https://ai.google.dev/x', 'status': 'RESOURCE_EXHAUSTED'}}"
    assert short_error(blob) == "quota or rate limit exceeded (429)"
    assert short_error("503 UNAVAILABLE. {...}") == "service overloaded (503)"
    assert short_error(ValueError("something odd happened " * 20)).endswith("…") and len(short_error(ValueError("x" * 500))) <= 160
    assert retry_delay("Please retry in 52.765977181s.") == pytest.approx(52.77, abs=0.01)
    assert retry_delay("'retryDelay': '53s'") == 53.0 and retry_delay("nothing here") is None


def test_only_the_three_primary_statements_are_scraped_as_a_fallback(tmp_path):
    e = FakeEdgar(Cache(None), tmp_path)
    filing = next(x for x in e.select_filings(e.recent_filings("1234567")) if x.form == "10-K")
    names = [
        "CONSOLIDATED STATEMENTS OF OPERATIONS", "CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME", "CONSOLIDATED BALANCE SHEETS",
        "CONSOLIDATED BALANCE SHEETS (Parenthetical)", "CONSOLIDATED STATEMENTS OF SHAREHOLDERS' EQUITY", "CONSOLIDATED STATEMENTS OF CASH FLOWS",
    ]
    reports = "".join(f"<Report><ShortName>{n}</ShortName><HtmlFileName>R{i}.htm</HtmlFileName><MenuCategory>Statements</MenuCategory></Report>" for i, n in enumerate(names, 2))
    e.site[f"{filing.base_url}/FilingSummary.xml"] = f"<FilingSummary><MyReports>{reports}</MyReports></FilingSummary>"
    assert [n for n, _ in e.statement_reports(filing)] == ["CONSOLIDATED STATEMENTS OF OPERATIONS", "CONSOLIDATED BALANCE SHEETS", "CONSOLIDATED STATEMENTS OF CASH FLOWS"]
