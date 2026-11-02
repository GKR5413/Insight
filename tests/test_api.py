import json

from fastapi.testclient import TestClient

from briefing.api import create_app
from briefing.embeddings import ConfigError

from .fakes import ScriptedSummarizer, doc_claim, make_services


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


def client(tmp_path):
    svc = make_services(tmp_path, ScriptedSummarizer(draft_fn=lambda reg, bf: [doc_claim(reg)]))
    return TestClient(create_app(factory=lambda: svc))


def test_stream_emits_progress_quote_then_result(tmp_path):
    r = client(tmp_path).get("/api/brief/stream", params={"ticker": "acme"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "result" and "progress" in kinds and "quote" in kinds
    result = events[-1][1]
    assert result["brief"]["ticker"] == "ACME" and result["quote"]["price"] == 123.45
    assert result["brief"]["sections"][0]["claims"][0]["citations"]


def test_blocking_endpoint_returns_quote_and_brief(tmp_path):
    body = client(tmp_path).get("/api/brief", params={"ticker": "ACME"}).json()
    assert set(body) == {"quote", "brief"} and body["brief"]["sources"]


def test_bad_tickers_are_rejected_before_any_work(tmp_path):
    c = client(tmp_path)
    for bad in ("", "../etc/passwd", "AAPL;DROP", "A" * 20, "1ABC"):
        assert c.get("/api/brief/stream", params={"ticker": bad}).status_code == 422


def test_unknown_ticker_is_an_error_event_not_a_crash(tmp_path):
    events = parse_sse(client(tmp_path).get("/api/brief/stream", params={"ticker": "NOPE"}).text)
    assert events[-1][0] == "error" and "No SEC registrant" in events[-1][1]["message"]


def test_incomplete_configuration_returns_actionable_503_and_health_explains(tmp_path):
    def factory():
        raise ConfigError("GOOGLE_API_KEY is not set. Add your Gemini API key to .env (see .env.example).")

    c = TestClient(create_app(factory=factory))
    r = c.get("/api/brief/stream", params={"ticker": "AAPL"})
    assert r.status_code == 503 and "GOOGLE_API_KEY" in r.json()["detail"]
    health = c.get("/api/health").json()
    assert health["ok"] is False and any("GOOGLE_API_KEY" in p for p in health["problems"])


def test_health_reports_backend_and_evidence_count(tmp_path):
    c = client(tmp_path)
    c.get("/api/brief", params={"ticker": "ACME"})
    h = c.get("/api/health").json()
    assert h["cache_backend"] == "memory" and h["evidence_rows"] > 0
