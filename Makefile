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
	signal-sprint $(DS) --inspect

analyze:        # make analyze DS=path/to/dataset.zip
	signal-sprint $(DS)
