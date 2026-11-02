"""Per-artifact graph: route by artifact kind -> parse -> embed & index.

Statement figures come from the local FSDS database when it has the filing (no network), and
fall back to scraping the filing's R pages when it does not.

The routing edge is where parser choice lives. PDFs go to a stub today; Phase 2 adds the local
parser and the Azure Document Intelligence adapter as additional branches here.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..models import Artifact, Record
from ..parsing.fsds_rows import fsds_records
from ..parsing.html_filing import parse_exhibit_html, parse_filing_html, sections_to_records
from ..parsing.pdf import PdfNotSupported, pdf_records
from ..parsing.statements import parse_statement_html, statement_records
from ..parsing.xlsx_upload import workbook_records
from ..services import Services


class ArtifactState(TypedDict, total=False):
    artifact: Artifact
    records: list[Record]
    warnings: list[str]
    indexed: int


def build_artifact_graph(svc: Services):
    def route(state: ArtifactState) -> str:
        return {"html": "parse_html", "exhibit": "parse_html", "statements": "parse_statements", "fsds": "parse_fsds", "xlsx": "parse_xlsx", "pdf": "parse_pdf"}[state["artifact"].kind]

    def parse_html(state: ArtifactState) -> ArtifactState:
        a = state["artifact"]
        f = a.filing
        html = svc.edgar.fetch_text(a.url)
        if a.kind == "exhibit":
            recs = parse_exhibit_html(html, f, a.ticker, a.url, Path(a.url).name)
        else:
            recs = sections_to_records(parse_filing_html(html, f.form), f, a.ticker, a.url)
        warn = [] if recs else [f"{a.label}: no extractable sections found"]
        return {"records": recs, "warnings": warn}

    def parse_statements(state: ArtifactState) -> ArtifactState:
        a = state["artifact"]
        f = a.filing
        recs: list[Record] = []
        for short, file in svc.edgar.statement_reports(f):
            url = f"{f.base_url}/{file}"
            _, rows = parse_statement_html(svc.edgar.fetch_text(url), short)
            recs.extend(statement_records(rows, f, a.ticker, short, file, url))
        return {"records": recs, "warnings": [] if recs else [f"{a.label}: no financial statement tables found"]}

    def parse_fsds(state: ArtifactState) -> ArtifactState:
        a = state["artifact"]
        recs = fsds_records(svc.fsds, a.filing, a.ticker) if svc.fsds else []
        return {"records": recs, "warnings": []}

    def after_fsds(state: ArtifactState) -> str:
        # A filing the data set lacks usable lines for falls back to scraping the R pages.
        return "index" if state.get("records") else "parse_statements"

    def parse_xlsx(state: ArtifactState) -> ArtifactState:
        a = state["artifact"]
        recs = workbook_records(Path(a.path), a.ticker)
        return {"records": recs, "warnings": [] if recs else [f"{a.label}: no tabular rows found"]}

    def parse_pdf(state: ArtifactState) -> ArtifactState:
        a = state["artifact"]
        try:
            return {"records": pdf_records(Path(a.path), a.ticker), "warnings": []}
        except PdfNotSupported as exc:
            return {"records": [], "warnings": [str(exc)]}

    def index(state: ArtifactState) -> ArtifactState:
        recs = state.get("records") or []
        if not recs:
            return {"indexed": 0}
        vectors = svc.embedder.embed_documents([r.embed_text() for r in recs])
        return {"indexed": svc.store.upsert(recs, vectors)}

    g = StateGraph(ArtifactState)
    for name, fn in (("parse_html", parse_html), ("parse_statements", parse_statements), ("parse_fsds", parse_fsds), ("parse_xlsx", parse_xlsx), ("parse_pdf", parse_pdf), ("index", index)):
        g.add_node(name, fn)
    g.add_conditional_edges(START, route, ["parse_html", "parse_statements", "parse_fsds", "parse_xlsx", "parse_pdf"])
    g.add_conditional_edges("parse_fsds", after_fsds, ["index", "parse_statements"])
    for name in ("parse_html", "parse_statements", "parse_xlsx", "parse_pdf"):
        g.add_edge(name, "index")
    g.add_edge("index", END)
    return g.compile()
