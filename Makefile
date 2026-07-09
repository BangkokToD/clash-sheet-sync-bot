.PHONY: check py_compile test lint format

check: py_compile test

py_compile:
	git ls-files '*.py' | xargs python -m py_compile

test:
	python -m pytest -q

lint:
	python -m ruff check .

format:
	python -m ruff format .
	python -m ruff check . --fix
