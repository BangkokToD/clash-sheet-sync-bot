.PHONY: check py_compile test lint format

check: py_compile test

py_compile:
	python -m py_compile *.py

test:
	python -m pytest -q

lint:
	python -m ruff check .

format:
	python -m ruff format .
	python -m ruff check . --fix
