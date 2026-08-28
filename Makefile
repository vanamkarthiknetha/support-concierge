.PHONY: help setup db run eval sweep chaos test api web lint summary clean demo

BACKEND := backend
UV := uv --directory $(BACKEND)

help:
	@echo "make setup    install deps + start Postgres + create schema"
	@echo "make test     162 tests — no API key needed"
	@echo "make eval     score against the gold set (non-zero exit on safety violation)"
	@echo "make run      triage all 18 sample tickets -> results/baseline.json"
	@echo "make sweep    eval + threshold frontier"
	@echo "make chaos    failure-injection runs -> results/chaos_*.json"
	@echo "make summary  regenerate results/SUMMARY.md from the run artifacts"
	@echo "make api      FastAPI on :8010 (docs at /docs)"
	@echo "make web      Next.js console on :3000"
	@echo "make lint     ruff + tsc"
	@echo "make demo     setup + run + eval, end to end"
	@echo "make clean    tear down Postgres and its volume"

setup:
	docker compose up -d
	$(UV) sync
	@echo "waiting for postgres..."
	@until docker exec concierge-db pg_isready -U concierge -d concierge >/dev/null 2>&1; do sleep 1; done
	$(UV) run concierge initdb

db:
	docker compose up -d

# Needs a GEMINI_API_KEY in .env.
run:
	$(UV) run concierge run --reset

# These two need no API key: they score already-committed run artifacts.
eval:
	$(UV) run python -m evals.run

sweep:
	$(UV) run python -m evals.run --sweep

test:
	$(UV) run pytest -q

summary:
	$(UV) run python scripts/make_summary.py

# Failure injection. Note `--no-persist` (Typer's boolean form — `--persist=false`
# is rejected) and the quoted `agent=*`, which the shell would otherwise glob.
chaos:
	$(UV) run concierge run --chaos "agent=classifier,mode=timeout,rate=1.0" \
		--no-persist --out ../results/chaos_classifier_timeout.json
	$(UV) run concierge run --chaos "agent=drafter,mode=malformed,rate=1.0" \
		--no-persist --out ../results/chaos_drafter_malformed.json
	$(UV) run concierge run --chaos "agent=critic,mode=error_500,rate=1.0" \
		--no-persist --out ../results/chaos_critic_error.json
	$(UV) run concierge run --chaos "agent=*,mode=error_500,rate=1.0" \
		--no-persist --out ../results/chaos_total_outage.json

api:
	$(UV) run uvicorn concierge.api.main:app --reload --port 8010

web:
	cd web && npm run dev

lint:
	$(UV) run ruff check concierge evals tests
	cd web && npx tsc --noEmit

clean:
	docker compose down -v

demo: setup run eval
	@echo ""
	@echo "Now:  make api   (terminal 2)"
	@echo "      make web   (terminal 3)  ->  http://localhost:3000"
