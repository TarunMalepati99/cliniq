.PHONY: install bootstrap test eval api web demo openapi diagrams
install:        ## install backend (editable) + frontend deps
	pip install -e "backend[dbt,dev,llm]" && cd frontend && npm ci
bootstrap:      ## migrate → generate → ingest → dbt → index
	cliniq bootstrap
test:           ## backend unit + integration tests (needs PostgreSQL; see README)
	cd backend && pytest
eval:           ## run all benchmark suites and write eval/reports/
	cliniq eval
api:
	cliniq serve --port 8000
web:
	cd frontend && npm run build && npm run start
demo:           ## headless end-to-end UI walkthrough with screenshots (API + web must be running)
	cd scripts && npm i --no-save playwright && cd .. && node scripts/demo_walkthrough.mjs
openapi:
	cliniq openapi
diagrams:
	for f in docs/diagrams/*.mmd; do mmdc -i $$f -o $${f%.mmd}.svg -b transparent; done
