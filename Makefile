PYTHON ?= $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/python.exe,$(if $(wildcard .venv/bin/python),.venv/bin/python,python))
CHECKED_PYTHON = src/mcps/development.py src/mcps/tunnels.py tests/test_development.py tests/test_tunnels.py

.PHONY: check
check:
	$(PYTHON) -m ruff check $(CHECKED_PYTHON)
	$(PYTHON) -m ruff format --check $(CHECKED_PYTHON)
	$(PYTHON) -m mypy
	$(PYTHON) -m pytest -q
	node --test tests/gateway.test.js
