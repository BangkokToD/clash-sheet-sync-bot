.PHONY: check py_compile test

check: py_compile test

py_compile:
	python -m py_compile *.py

test:
	python -m pytest -q
