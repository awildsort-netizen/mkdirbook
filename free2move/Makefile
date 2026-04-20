# free2move -- local book build
#
# Targets:
#   make / make out     Export html and docx
#   make html/docx      Export a single format

PYTHON   := ../.venv/bin/python
BOOKCC   := ../scripts/bookcc.py
MANIFEST := free2move.json

export PYTHONPYCACHEPREFIX := ../scripts/__pycache__

.DEFAULT_GOAL := out

.PHONY: out html docx

out: html docx

html: $(PYTHON)
	$(PYTHON) $(BOOKCC) $(MANIFEST) -f html

docx: $(PYTHON)
	$(PYTHON) $(BOOKCC) $(MANIFEST) -f docx

$(PYTHON):
	@echo "Virtual environment not found. Run: make install"
	@exit 1
