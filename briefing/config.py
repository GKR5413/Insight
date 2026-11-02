"""Application settings.

Everything service-shaped (LangSmith, OpenAI, Finnhub, Azure) is optional and degrades
gracefully when its key is absent. Only the SEC user agent and, unless you run in
offline mode, a Gemini key are required.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Exports .env into os.environ so LangChain / LangSmith see GOOGLE_API_KEY, LANGSMITH_* etc.
load_dotenv(ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", populate_by_name=True)

    # --- SEC EDGAR ---------------------------------------------------------------
    # The SEC requires a descriptive User-Agent with a contact, e.g. "Acme Wealth you@acme.com".
    sec_user_agent: str = ""
    filings_lookback_days_8k: int = 120
    max_8k: int = 4

    # --- Models ------------------------------------------------------------------
    google_api_key: str = Field("", validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY"))
    llm_provider: str = "gemini"  # gemini | offline (extractive, no LLM; for demos and tests)
    llm_model: str = "gemini-flash-latest"
    # Tried in order when the primary is overloaded (503), over quota (429) or retired (404).
    llm_fallback_models: str = "gemini-3.6-flash,gemini-flash-lite-latest"
    embeddings_provider: str = "gemini"  # gemini | openai | fake
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    # Texts per minute the embedding API allows. Gemini's free tier is 100; set 0 for no pacing (paid tiers).
    embedding_rpm: int = 80
    openai_api_key: str = ""

    # --- Vector store / cache ----------------------------------------------------
    data_dir: Path = ROOT / "data"
    milvus_uri: str = ""  # default: <data_dir>/milvus.db (Milvus Lite). Or http://localhost:19530
    redis_url: str = "redis://localhost:6379/0"
    brief_cache_ttl: int = 24 * 3600
    quote_cache_ttl: int = 60

    # --- Quotes ------------------------------------------------------------------
    quote_providers: str = "finnhub,cboe,yfinance"  # tried in order; finnhub skipped without a key
    finnhub_api_key: str = ""

    # --- Bulk statement figures (SEC Financial Statement Data Sets in SQLite) ---------
    use_fsds: bool = True  # used automatically when the SQLite file exists; otherwise R pages are scraped
    fsds_path: str = ""  # default: <data_dir>/fsds.sqlite

    # --- Retrieval ---------------------------------------------------------------
    facet_top_k: int = 6

    # --- Observability (picked up by LangChain from the environment) ---------------
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "stock-briefing"

    # --- API ---------------------------------------------------------------------
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def resolved_milvus_uri(self) -> str:
        return self.milvus_uri or str(self.data_dir / "milvus.db")

    @property
    def edgar_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "edgar"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def fsds_raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def fsds_db(self) -> Path:
        return Path(self.fsds_path) if self.fsds_path else self.data_dir / "fsds.sqlite"


@lru_cache
def get_settings() -> Settings:
    return Settings()
