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

docker-up:      # start (./watchover.sh install does the same with .env generation)
	./watchover.sh start

docker-update:  # new source tree copied over this folder: rebuild the image and recreate the containers; data stays in the volumes
	./watchover.sh update

docker-logs:
	docker compose logs -f dashboard

docker-migrate:  # SQLite dosyalarını (data birimindeki) PostgreSQL'e kopyala
	docker compose exec dashboard python -m watchover.migrate --source /data

docker-backup:   # PostgreSQL + veri birimi -> backups/watchover-YYYYMMDD-HHMM.tgz
	./watchover.sh backup

docker-down:
	docker compose down
