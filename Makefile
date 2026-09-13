.PHONY: test demo inspect analyze
test:
	pytest -q
demo:
	python3 samples/make_demo.py && python3 signal_sprint.py serve samples/demo_mixed.zip
inspect:
	python3 signal_sprint.py inspect $(DS)
analyze:
	python3 signal_sprint.py analyze $(DS)
