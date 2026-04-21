# mkdirbook -- project-level helpers
#
# Targets:
#   make / make out      Build all books (html and docx)
#   make setup          Create .venv, install deps, download TiddlyWiki shell
#   make gui            Launch the manifest manager TUI
#   make clean          Remove generated output
#   make help           Show book build commands

BOOKCC  := scripts/bookcc.py
BOOKMAN := scripts/bookman.py
BOOKDIR := .
BOOK_DIRS := $(sort $(patsubst %/Makefile,%,$(wildcard */Makefile)))
SETUP_STAMP := .venv/.setup-ready
VENV_DIR := $(abspath .venv)
VENV_BIN := $(abspath .venv/bin)
ACTIVATE := $(VENV_BIN)/activate

export PYTHONPYCACHEPREFIX := scripts/__pycache__
export PATH := $(VENV_BIN):$(PATH)

.DEFAULT_GOAL := out

.PHONY: help gui setup out clean

out: $(SETUP_STAMP)
	@for dir in $(BOOK_DIRS); do \
		$(MAKE) -C "$$dir"; \
	done

gui: $(SETUP_STAMP)
	./$(BOOKMAN) $(BOOKDIR)

help:
	@echo "Project commands:"
	@echo "  make"
	@echo "  make out"
	@echo "  make setup"
	@echo "  make gui"
	@echo "  cd free2move && make"
	@echo "  cd newsletters && make"

# Set up the virtual environment and install dependencies
setup: $(SETUP_STAMP)
	@if [ "$$VIRTUAL_ENV" = "$(VENV_DIR)" ]; then \
		echo "Project virtual environment already active: $(VENV_DIR)"; \
	else \
		echo "Opening shell with $(VENV_DIR) activated. Use 'exit' to leave."; \
		. "$(ACTIVATE)"; \
		exec "$${SHELL:-/bin/sh}" -i; \
	fi

$(SETUP_STAMP): Makefile scripts/requirements.txt
	python3 -m venv .venv
	.venv/bin/pip install -r scripts/requirements.txt
	chmod +x $(BOOKCC) $(BOOKMAN) scripts/aswritten.py
	@echo "Downloading TiddlyWiki empty shell..."
	curl -L -o templates/tiddlywiki_empty.html https://tiddlywiki.com/empty.html
	@if ! command -v pandoc >/dev/null 2>&1; then \
		echo "pandoc not found."; \
		if command -v brew >/dev/null 2>&1; then \
			printf "Install pandoc with Homebrew now? [y/N] "; \
			read answer; \
			case "$$answer" in \
				[Yy]*) brew install pandoc ;; \
				*) echo "pandoc is required for DOCX builds. Run: brew install pandoc"; exit 1 ;; \
			esac; \
		elif command -v apt-get >/dev/null 2>&1; then \
			printf "Install pandoc with apt-get now? [y/N] "; \
			read answer; \
			case "$$answer" in \
				[Yy]*) sudo apt-get update && sudo apt-get install -y pandoc ;; \
				*) echo "pandoc is required for DOCX builds. Run: sudo apt-get update && sudo apt-get install -y pandoc"; exit 1 ;; \
			esac; \
		else \
			echo "pandoc is required for DOCX builds."; \
			echo "Install it with Homebrew or apt-get and rerun make."; \
			exit 1; \
		fi; \
	fi
	touch $(SETUP_STAMP)

# Sentinel: remind the user to run make setup if .venv is missing
.venv:
	@echo "Virtual environment not found. Run: make setup"
	@exit 1

# Remove generated output
clean:
	rm -rf out/
