# mkdirbook -- project-level helpers
#
# Targets:
#   make install        Create .venv, install deps, download TiddlyWiki shell
#   make / make gui     Launch the manifest manager TUI
#   make clean          Remove generated output
#   make help           Show book build commands

PYTHON  := .venv/bin/python
BOOKCC  := scripts/bookcc.py
BOOKMAN := scripts/bookman.py
BOOKDIR := .

export PYTHONPYCACHEPREFIX := scripts/__pycache__

.DEFAULT_GOAL := help

.PHONY: help gui install clean

gui: .venv
	$(PYTHON) $(BOOKMAN) $(BOOKDIR)

help:
	@echo "Project commands:"
	@echo "  make install"
	@echo "  make gui"
	@echo "  cd free2move && make"
	@echo "  cd newsletters && make"

# Set up the virtual environment and install dependencies
install:
	python3 -m venv .venv
	.venv/bin/pip install -r scripts/requirements.txt
	chmod +x $(BOOKCC) $(BOOKMAN)
	@echo "Downloading TiddlyWiki empty shell..."
	curl -L -o templates/tiddlywiki_empty.html https://tiddlywiki.com/empty.html

# Sentinel: remind the user to run make install if .venv is missing
.venv:
	@echo "Virtual environment not found. Run: make install"
	@exit 1

# Remove generated output
clean:
	rm -rf out/
