# Usage: make setup | make api | make web | make build | make test | make check | make prewarm T="AAPL MSFT"
PY := .venv/bin/python

setup:           ## create the venv and install everything (needs uv: pip install uv)
	uv venv --python 3.12 .venv
	VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
	cd frontend && npm install

api:             ## API + (if built) the SPA on http://localhost:8000
	$(PY) -m uvicorn briefing.api:app --reload --port 8000 --timeout-graceful-shutdown 5

web:             ## SPA dev server with hot reload on http://localhost:5173 (proxies /api to :8000)
	cd frontend && npm run dev

build:           ## type-check and build the SPA into frontend/dist (served by `make api`)
	cd frontend && npm run build

test:
	$(PY) -m pytest -q

check:           ## preflight the Gemini path (costs a fraction of a cent)
	$(PY) scripts/check_gemini.py

prewarm:         ## index and brief tickers ahead of a demo, e.g. make prewarm T="AAPL MSFT JPM"
	$(PY) scripts/prewarm.py $(T)

.PHONY: setup api web build test check prewarm
