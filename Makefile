.PHONY: install run test demo inspect analyze

install:
	pip install -e ".[dev]"

run:            # streamlit dashboard on :8501 (upload from the sidebar, or click "Load demo dataset")
	streamlit run app.py

demo:
	python3 samples/make_demo.py && streamlit run app.py

test:
	python3 -m pytest -q

inspect:        # make inspect DS=path/to/dataset.zip
	watchover $(DS) --inspect

analyze:        # make analyze DS=path/to/dataset.zip
	watchover $(DS)

# ---- Docker (docs/KURULUM_DOCKER.md)
docker-build:   # local image tagged watchover:local (GIT_REV baked in)
	docker build --build-arg GIT_REV=$$(git rev-parse --short HEAD) --build-arg VERSION=local -t watchover:local .

docker-up:      # start (pulls the published image unless WATCHOVER_IMAGE points elsewhere)
	docker compose up -d

docker-update:  # new version: pull the image and recreate the container; data stays in the volume
	docker compose pull && docker compose up -d && docker image prune -f

docker-logs:
	docker compose logs -f dashboard

docker-down:
	docker compose down
