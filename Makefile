.PHONY: dev api web test demo install

install:
	cd backend && pip install -e ".[dev]"
	cd frontend && npm install

api:
	cd backend && uvicorn app.api.main:app --reload --port 8000

web:
	cd frontend && npm run dev

dev:
	$(MAKE) -j2 api web

test:
	cd backend && pytest -q

demo: dev
