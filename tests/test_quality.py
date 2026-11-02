import uuid

import pytest

from briefing import quality
from briefing.graphs.brief import build_brief_graph
from briefing.run import stream_brief

from .fakes import ScriptedSummarizer, bad_claim, doc_claim, make_services, sheet_claim


def run(svc, ticker="ACME", refresh=False):
    return list(stream_brief(svc, build_brief_graph(svc), ticker, refresh))


def log(svc):
    return quality.load(svc.settings.data_dir / "quality")


def test_a_brief_writes_one_quality_record_with_the_first_pass_numbers(tmp_path):
    summ = ScriptedSummarizer(
        draft_fn=lambda reg, bf: [doc_claim(reg), sheet_claim(reg), bad_claim(reg)],
        repair_fn=lambda reg, bf, failures: [doc_claim(reg, facet="risks", text="Fixed.")],
    )
    svc = make_services(tmp_path, summ)
    run(svc)
    (r,) = log(svc)
    assert (r["outcome"], r["ticker"], r["claims_drafted"], r["first_pass_failed"], r["retries"], r["removed"], r["final_claims"]) == ("ok", "ACME", 3, 1, 1, 0, 3)
    assert r["first_pass_rate"] == pytest.approx(0.667, abs=0.001)  # the honest model-quality number, before the retry hides the miss
    assert r["complete"] is True and r["llm_used"] == "scripted" and r["duration_s"] >= 0
    uuid.UUID(r["run_id"])  # the id handed to LangGraph, so LangSmith feedback can attach to this trace


def test_failures_are_logged_with_a_cause_not_just_a_message(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda r, b: []))
    run(svc, ticker="NOPE")
    (r,) = log(svc)
    assert (r["outcome"], r["error_kind"]) == ("error", "unknown_ticker")
    assert quality.classify_error("Embedding quota used up (Gemini free tier: 1,000 texts/day); resets in ~11h37m.") == "embedding_quota"
    assert quality.classify_error("Gemini could not answer right now (a: service overloaded (503))") == "llm_unavailable"
    assert quality.classify_error("No evidence could be retrieved (x)") == "no_evidence"
    assert quality.classify_error("Cannot open Milvus at http://localhost:19530") == "config"
    assert quality.classify_error("who knows") == "other"


def test_cache_hits_are_counted_but_do_not_pose_as_quality_samples(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)]))
    run(svc)
    run(svc)
    assert [r["outcome"] for r in log(svc)] == ["ok", "cache_hit"]


class FakeLangSmith:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def create_feedback(self, **kw):
        if self.fail:
            raise RuntimeError("network down")
        self.calls.append(kw)


def test_scores_are_attached_to_the_langsmith_run_only_when_tracing_is_on(tmp_path, monkeypatch):
    rec = {"outcome": "ok", "run_id": "rid-1", "complete": False, "retries": 1, "removed": 2, "final_claims": 7, "first_pass_rate": 0.75}
    fake = FakeLangSmith()
    monkeypatch.setattr(quality, "langsmith_enabled", lambda: False)
    quality.record_run(tmp_path, rec, client=fake)
    assert fake.calls == []  # tracing off: nothing is sent anywhere
    monkeypatch.setattr(quality, "langsmith_enabled", lambda: True)
    quality.record_run(tmp_path, rec, client=fake)
    sent = {c["key"]: c["score"] for c in fake.calls}
    assert sent == {"complete": 0.0, "retried": 1.0, "removed": 2.0, "final_claims": 7.0, "first_pass_rate": 0.75}
    assert {c["run_id"] for c in fake.calls} == {"rid-1"}


def test_a_langsmith_outage_never_breaks_a_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "langsmith_enabled", lambda: True)
    rec = {"outcome": "ok", "run_id": "rid", "complete": True, "retries": 0, "removed": 0, "final_claims": 1, "first_pass_rate": 1.0}
    quality.record_run(tmp_path, rec, client=FakeLangSmith(fail=True))  # must not raise
    assert len(quality.load(tmp_path)) == 1  # and the local record is still written


def test_report_states_rates_by_model_failures_and_what_is_not_yet_measured():
    ok = lambda ts, model, drafted, failed, removed=0, complete=True: {  # noqa: E731
        "ts": ts, "source": "live", "ticker": "AAPL", "outcome": "ok", "llm_used": model, "claims_drafted": drafted, "first_pass_failed": failed,
        "first_pass_rate": 1 - failed / drafted, "retries": 1 if failed else 0, "removed": removed, "final_claims": drafted - removed,
        "complete": complete, "duration_s": 60.0}
    records = [ok("2026-11-02T14:00:00+00:00", "flash-lite", 12, 3), ok("2026-11-02T15:00:00+00:00", "flash-lite", 8, 1, removed=1, complete=False),
               {"ts": "2026-11-02T16:00:00+00:00", "source": "live", "ticker": "MSFT", "outcome": "error", "error_kind": "embedding_quota", "duration_s": 5.0},
               {"ts": "2026-11-02T16:05:00+00:00", "outcome": "cache_hit", "ticker": "AAPL"}]
    golden = [{"ran_at": "2026-11-02T14:02:21", "results": [{"id": "SEC-01", "model": "flash-lite", "correct": True, "cited": True}, {"id": "SEC-13", "model": "", "correct": False}]}]
    text = quality.render_report(records, golden, {"project": "stock-briefing", "project_found": False}, generated="2026-11-02")
    assert "**80%**" in text and "of 20 drafted claims" in text  # 1 - 4/20
    assert "`flash-lite` | 2 | 20 | 80%" in text
    assert "embedding_quota | 1" in text and "capacity problems" in text
    assert "no traces have been sent" in text and "1 served from cache" in text
    assert "1 passed of 2 questions run" in text and "not a quality measurement yet" in text
    assert "anecdotal" in text
