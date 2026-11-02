"""The brief graph end to end, with a fake EDGAR and scripted summarizers (no network, no API keys)."""
import openpyxl
import pytest

from briefing.embeddings import Embedder
from briefing.graphs.artifact import build_artifact_graph
from briefing.graphs.brief import build_brief_graph
from briefing.models import Artifact, Filing
from briefing.run import stream_brief

from .fakes import ACC_K, FakeQuotes, ScriptedSummarizer, bad_claim, doc_claim, make_services, sheet_claim


def run(svc, summarizer=None, refresh=False):
    graph = build_brief_graph(svc)
    events = list(stream_brief(svc, graph, "ACME", refresh))
    return events, next((e for e in events if e["type"] == "result"), None), next((e for e in events if e["type"] == "error"), None)


def test_repair_loop_fixes_a_failed_claim_on_the_single_retry(tmp_path):
    summ = ScriptedSummarizer(
        draft_fn=lambda reg, bf: [doc_claim(reg), sheet_claim(reg), bad_claim(reg)],
        repair_fn=lambda reg, bf, failures: [doc_claim(reg, facet=failures[0]["claim"]["facet"], text="Corrected risk statement.")],
    )
    _, result, err = run(make_services(tmp_path, summ))
    assert err is None
    brief = result["brief"]
    assert brief["meta"]["first_pass_failed"] == 1 and brief["meta"]["retries"] == 1
    assert brief["removed_claims"] == 0
    assert summ.draft_calls == 1 and summ.repair_calls == 1
    texts = [c["text"] for s in brief["sections"] for c in s["claims"]]
    assert "Corrected risk statement." in texts and "An invented statement." not in texts


def test_claims_that_still_fail_after_one_retry_are_dropped_and_counted(tmp_path):
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg), bad_claim(reg)], repair_fn=lambda reg, bf, f: [bad_claim(reg)])
    _, result, _ = run(make_services(tmp_path, summ))
    brief = result["brief"]
    assert summ.repair_calls == 1  # exactly one retry, never a loop
    assert brief["removed_claims"] == 1
    assert [s["facet"] for s in brief["sections"]] == ["business"]


def test_brief_carries_section_level_citations_with_urls_and_sheet_rows(tmp_path):
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg), sheet_claim(reg)])
    _, result, _ = run(make_services(tmp_path, summ))
    sources = {s["ref"]: s for s in result["brief"]["sources"]}
    doc = next(s for s in sources.values() if s["kind"] == "doc")
    sheet = next(s for s in sources.values() if s["kind"] == "sheet")
    assert doc["url"].startswith("https://www.sec.gov/Archives/edgar/data/1234567/") and doc["item"].startswith("Item")
    assert sheet["row"] and sheet["file"].startswith("R") and "row" in sheet["label"]
    for sec in result["brief"]["sections"]:
        for claim in sec["claims"]:
            assert all(c["ref"] in sources for c in claim["citations"])


def test_second_run_is_served_from_cache_without_calling_the_model(tmp_path):
    quotes = FakeQuotes()
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)])
    svc = make_services(tmp_path, summ, quotes=quotes)
    _, first, _ = run(svc)
    _, second, _ = run(svc)
    assert first["brief"]["meta"]["cache_hit"] is False and second["brief"]["meta"]["cache_hit"] is True
    assert summ.draft_calls == 1
    assert second["quote"]["price"] == 123.45  # the quote branch still runs on a cache hit


def test_refresh_bypasses_the_brief_cache(tmp_path):
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)])
    svc = make_services(tmp_path, summ)
    run(svc)
    run(svc, refresh=True)
    assert summ.draft_calls == 2


def test_already_indexed_filings_are_not_fetched_again(tmp_path):
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)])
    svc = make_services(tmp_path, summ)
    run(svc)
    doc_fetches = lambda: [u for u in svc.edgar.requests if u.endswith("acme-10k.htm")]  # noqa: E731
    svc.edgar.requests.clear()
    svc.cache.delete(next(k for k in list(svc.cache._mem) if k.startswith("brief:")))  # force re-brief, keep Milvus
    run(svc)
    assert doc_fetches() == []  # served by the vector store, not by EDGAR


def test_a_failing_quote_provider_does_not_sink_the_brief(tmp_path):
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)])
    _, result, err = run(make_services(tmp_path, summ, quotes=FakeQuotes(fail=True)))
    assert err is None and result["quote"] is None
    assert any("Quote unavailable" in w for w in result["run_warnings"])
    assert result["brief"]["sections"]


def test_when_nothing_can_be_indexed_the_user_gets_a_clear_error(tmp_path, monkeypatch):
    class Boom:
        def embed_documents(self, texts):
            raise RuntimeError("embedding service down")

        def embed_query(self, text):
            raise RuntimeError("embedding service down")

    monkeypatch.setattr("briefing.embeddings.time.sleep", lambda s: None)
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: []), embedder=Embedder(Boom(), "boom", 128))
    _, result, err = run(svc)
    assert result is None
    assert err["message"].startswith("No evidence could be retrieved") and "embedding service down" in err["message"]


def test_no_surviving_claims_is_reported_not_hidden(tmp_path):
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [bad_claim(reg)])
    _, result, _ = run(make_services(tmp_path, summ))
    b = result["brief"]
    assert b["sections"] == [] and b["removed_claims"] == 1
    assert any("No claim survived" in w for w in b["warnings"])


def test_progress_events_cover_every_stage(tmp_path):
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)])
    events, _, _ = run(make_services(tmp_path, summ))
    stages = {e["stage"] for e in events if e["type"] == "progress"}
    assert {"resolve", "quote", "ingest", "retrieve", "summarize", "verify", "finalize"} <= stages
    assert any(e["type"] == "quote" for e in events)


def test_artifact_graph_routes_xlsx_and_pdf_and_reports_pdf_as_skipped(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: []))
    graph = build_artifact_graph(svc)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws.append(["Metric", "FY2024", "FY2025"])
    ws.append(["Revenue", 1000, 1200])
    p = tmp_path / "model.xlsx"
    wb.save(p)
    out = graph.invoke({"artifact": Artifact("xlsx", "ACME", None, path=str(p), label="model.xlsx")})
    assert out["indexed"] == 1
    hits = svc.retriever.retrieve("ACME", [out["records"][0].accession])[0]
    assert next(iter(hits.values()))["row"] == 2  # Excel-visible row

    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = graph.invoke({"artifact": Artifact("pdf", "ACME", None, path=str(pdf), label="deck.pdf")})
    assert out["indexed"] == 0 and "Phase 2" in out["warnings"][0]


def test_html_artifact_indexes_only_wanted_sections(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: []))
    f = next(f for f in svc.edgar.select_filings(svc.edgar.recent_filings("1234567")) if f.accession == ACC_K)
    out = build_artifact_graph(svc).invoke({"artifact": Artifact("html", "ACME", f, url=f.doc_url, label="10-K")})
    keys = {r.section_key for r in out["records"]}
    assert keys == {"10-K:1", "10-K:1A", "10-K:7"}
    assert svc.store.has(f.accession, "html") and not svc.store.has(f.accession, "sec_statement")


@pytest.mark.parametrize("n", [1])
def test_vector_search_is_scoped_to_ticker_and_accession(tmp_path, n):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: [doc_claim(r)]))
    run(svc)
    reg, _ = svc.retriever.retrieve("ACME", ["0000000000-00-000000"])
    assert reg == {}
    reg, _ = svc.retriever.retrieve("OTHER", [ACC_K])
    assert reg == {}


def test_a_brief_built_with_a_failed_source_is_flagged_and_not_cached(tmp_path, monkeypatch):
    """If a filing could not be indexed, the next lookup must retry it instead of replaying a thin brief."""
    from briefing.embeddings import HashingEmbeddings

    class FailsOnTenK(HashingEmbeddings):
        def embed_documents(self, texts):
            if any("Supply chain risk" in t for t in texts):  # only the 10-K fixture contains this
                raise RuntimeError("503 UNAVAILABLE")
            return super().embed_documents(texts)

    monkeypatch.setattr("briefing.embeddings.time.sleep", lambda s: None)
    summ = ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)])
    svc = make_services(tmp_path, summ, embedder=Embedder(FailsOnTenK(128), "hashing", 128))
    _, first, _ = run(svc)
    b = first["brief"]
    assert b["meta"]["complete"] is False
    assert any("could not be indexed" in w and "service overloaded (503)" in w for w in b["warnings"])
    _, second, _ = run(svc)
    assert second["brief"]["meta"]["cache_hit"] is False  # not replayed from cache
    assert summ.draft_calls == 2


def test_store_reloads_a_released_collection_and_retries(tmp_path):
    """Seen live: Milvus Lite's connection reset and the collection came back 'released'."""
    from pymilvus.exceptions import MilvusException

    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: []))
    store = svc.store
    store.upsert([doc_record()], [[0.1] * 128])

    class Proxy:
        """Real client, except the first search reports a released collection."""

        def __init__(self, real):
            self.real, self.failed, self.loads = real, False, 0

        def search(self, *a, **kw):
            if not self.failed:
                self.failed = True
                raise MilvusException(code=101, message="Collection 'x' is in state 'released'; call load() before search/get/query")
            return self.real.search(*a, **kw)

        def load_collection(self, name):
            self.loads += 1
            return self.real.load_collection(name)

        def __getattr__(self, n):
            return getattr(self.real, n)

    store.client = proxy = Proxy(store.client)
    hits = store.search([0.1] * 128, 'ticker == "ACME"', 3)
    assert [h["id"] for h in hits] == ["r1"] and proxy.loads == 1


def doc_record():
    from briefing.models import Record

    return Record(id="r1", ticker="ACME", accession="a", form="10-K", filed="2025-01-01", source_type="doc", section_key="10-K:1", item="Item 1", text="t")


def test_embedding_quota_waits_are_visible_in_the_progress_stream(tmp_path, monkeypatch):
    from briefing.embeddings import HashingEmbeddings, RateLimiter

    clock = {"t": 0.0}
    monkeypatch.setattr("briefing.embeddings.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s))  # virtual time
    emb = Embedder(HashingEmbeddings(128), "hashing", 128, rpm=10)
    emb._limiter = RateLimiter(10, now=lambda: clock["t"])
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)]), embedder=emb)
    events, result, err = run(svc)
    assert err is None and result is not None
    assert any(e["type"] == "progress" and e["message"].startswith("Pausing") and "embedding quota" in e["message"] for e in events)


def test_daily_quota_fails_fast_with_a_clear_message_instead_of_waiting_it_out(tmp_path, monkeypatch):
    from briefing.embeddings import HashingEmbeddings, QuotaExhausted

    sleeps: list[float] = []
    monkeypatch.setattr("briefing.embeddings.time.sleep", sleeps.append)

    class OverDailyCap(HashingEmbeddings):
        def embed_documents(self, texts):
            raise RuntimeError("429 RESOURCE_EXHAUSTED. {'message': 'Embed content requests per day limit exceeded. Please retry in 11h40m8.69s.', 'retryDelay': '42008s'}")

        embed_query = embed_documents

    emb = Embedder(OverDailyCap(128), "capped", 128)
    with pytest.raises(QuotaExhausted, match=r"Gemini free tier: 1,000 texts/day\); resets in ~11h40m"):
        emb.embed_query("anything")
    assert sleeps == []  # no 75-second waits for a quota that clears tomorrow
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: []), embedder=emb)
    _, result, err = run(svc)
    assert result is None and "resets in ~11h40m" in err["message"]


def test_facet_query_vectors_are_cached_on_disk_so_retrieval_needs_no_embedding_calls(tmp_path):
    from briefing.facets import Retriever

    calls = {"n": 0}
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: [doc_claim(r)]))
    real = svc.embedder.embed_query
    svc.embedder.embed_query = lambda q: (calls.__setitem__("n", calls["n"] + 1), real(q))[1]
    cache = tmp_path / "queries.json"
    Retriever(svc.store, svc.embedder, cache).retrieve("ACME", ["x"])  # nothing indexed: still embeds each facet query once
    first = calls["n"]
    assert first > 0 and cache.exists()
    Retriever(svc.store, svc.embedder, cache).retrieve("ACME", ["x"])  # a fresh process (new Retriever) reads the file
    assert calls["n"] == first


def test_exhibits_are_capped_to_protect_embedding_quota():
    from briefing.parsing.html_filing import MAX_EXHIBIT_PASSAGES, parse_exhibit_html

    big = "<html><body>" + "".join(f"<div>Paragraph {i}. " + "Supplemental disclosure text that goes on for a while. " * 30 + "</div>" for i in range(80)) + "</body></html>"
    f = Filing(cik="1", accession="0000000001-25-000001", form="8-K", filed="2025-01-01", report_date="", primary_doc="x.htm")
    assert len(parse_exhibit_html(big, f, "ACME", "u", "ex992.htm")) == MAX_EXHIBIT_PASSAGES


def test_once_the_daily_quota_is_known_to_be_gone_every_later_call_fails_without_touching_the_provider(monkeypatch):
    from briefing.embeddings import QuotaExhausted

    monkeypatch.setattr("briefing.embeddings.time.sleep", lambda s: None)

    class Backend:
        calls = 0

        def embed_documents(self, texts):
            Backend.calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED per day. Please retry in 40000s.")

        embed_query = embed_documents

    emb = Embedder(Backend(), "m", 4, rpm=80)
    for _ in range(4):
        with pytest.raises(QuotaExhausted):
            emb.embed_documents(["a", "b"])
    assert Backend.calls == 1  # only the first call reached the provider
