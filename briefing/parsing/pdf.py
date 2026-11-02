"""PDF parsing is Phase 2.

Plan: a local parser (PyMuPDF text + pdfplumber tables, Tesseract for scans) as the default and
an Azure AI Document Intelligence adapter behind AZURE_DI_* settings for complex layouts. PDF
citations will be file + page + heading. Until then PDFs are skipped with a visible warning
rather than silently ignored.
"""
from __future__ import annotations

from pathlib import Path

from ..models import Record


class PdfNotSupported(NotImplementedError):
    pass


def pdf_records(path: Path, ticker: str) -> list[Record]:
    raise PdfNotSupported(f"{Path(path).name}: PDF parsing arrives in Phase 2 and was skipped.")
