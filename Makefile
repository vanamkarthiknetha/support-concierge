.PHONY: help setup db run eval sweep chaos test api web lint clean demo

BACKEND := backend
UV := uv --directory $(BACKEND)

help:
	@echo "setup   install deps + start Postgres + create schema"
	@echo "run     triage all 18 sample tickets -> results/baseline.json"
	@echo "eval    score against the gold set (non-zero exit on safety violation)"
	@echo "sweep   eval + threshold frontier"
	@echo "chaos   failure-injection runs -> results/chaos_*.json"
	@echo "test    162 tests, no API key needed"
	@echo "api     FastAPI on :8010 (docs at /docs)"
	@echo "web     Next.js console on :3000"
	@echo "demo    setup + run + eval, end to end"

setup:
	docker compose up -d
	$(UV) sync
	@echo "waiting for postgres..."
	@until docker exec concierge-db pg_isready -U concierge -d concierge >/dev/null 2>&1; do sleep 1; done
	$(UV) run concierge initdb

db:
	docker compose up -d

run:
	$(UV) run concierge run --reset

eval:
	$(UV) run python -m evals.run

sweep:
	$(UV) run python -m evals.run --sweep

chaos:
	$(UV) run concierge run --chaos agent=classifier,mode=timeout,rate=1.0 \
		--out results/chaos_classifier_timeout.json --persist=false
	$(UV) run concierge run --chaos agent=drafter,mode=malformed,rate=1.0 \
		--out results/chaos_drafter_malformed.json --persist=false
	$(UV) run concierge run --chaos agent=critic,mode=error_500,rate=1.0 \
		--out results/chaos_critic_error.json --persist=false
	$(UV) run concierge run --chaos agent=*,mode=error_500,rate=0.3 \
		--out results/chaos_flaky_all.json --persist=false

test:
	$(UV) run pytest -q

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
