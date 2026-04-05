# Free to Move -- book project
#
# Targets:
#   make install        Create .venv, install deps
#   make / make gui     Launch the manifest manager TUI (works launcher)
#   make out            Export all formats (pdf html md docx)
#   make pdf/html/md/docx  Single-format export
#   make check          Dry-run: show what would be compiled
#   make clean          Remove out/ build artifacts
#
# Override the manifest:  make pdf MANIFEST=works/other.json

PYTHON   := .venv/bin/python
BOOKCC   := scripts/bookcc.py
BOOKMAN  := scripts/bookman.py
MANIFEST := works/free2move.json
BOOKDIR  := .

export PYTHONPYCACHEPREFIX := scripts/__pycache__

.PHONY: gui out pdf html md docx install clean check

# Default target: open the manifest manager TUI
gui: .venv
	$(PYTHON) $(BOOKMAN) $(BOOKDIR)

# Export all formats
out: .venv
	$(PYTHON) $(BOOKCC) $(MANIFEST) -f pdf,html,md,docx

pdf: .venv
	$(PYTHON) $(BOOKCC) $(MANIFEST) -f pdf

html: .venv
	$(PYTHON) $(BOOKCC) $(MANIFEST) -f html

md: .venv
	$(PYTHON) $(BOOKCC) $(MANIFEST) -f md

docx: .venv
	$(PYTHON) $(BOOKCC) $(MANIFEST) -f docx

# Dry run: show what would be compiled without writing anything
check: .venv
	$(PYTHON) $(BOOKCC) $(MANIFEST) -n -v

# Set up the virtual environment and install dependencies
install:
	python3 -m venv .venv
	.venv/bin/pip install -r scripts/requirements.txt
	chmod +x $(BOOKCC) $(BOOKMAN)

# Sentinel: remind the user to run make install if .venv is missing
.venv:
	@echo "Virtual environment not found. Run: make install"
	@exit 1

# Remove generated output
clean:
	rm -rf out/
