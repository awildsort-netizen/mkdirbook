# newsletters -- local book build
#
# Targets:
#   make / make out     Export html and docx
#   make html/docx      Export a single format

BOOKCC   := ../scripts/bookcc.py
MANIFEST := newsletter.json
VENV_PY  := ../.venv/bin/python3
VENV_BIN := $(abspath ../.venv/bin)

export PYTHONPYCACHEPREFIX := ../scripts/__pycache__
export PATH := $(VENV_BIN):$(PATH)

.DEFAULT_GOAL := out

.PHONY: out html docx

out: html docx

html: $(VENV_PY)
	$(BOOKCC) $(MANIFEST) -f html

docx: $(VENV_PY)
	$(BOOKCC) $(MANIFEST) -f docx

$(VENV_PY):
	@echo "Virtual environment not found. Run: make setup"
	@exit 1
