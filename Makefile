# Free to Move -- book project
#
# Targets:
#   make install      Create .venv, install deps, chmod +x the script
#   make / make gui   Launch the manifest manager GUI
#   make out          Export the book to out/ (pdf, html, docx, md)
#   make clean        Remove out/ build artifacts

PYTHON  := .venv/bin/python
SCRIPT  := scripts/bookman.py
BOOKDIR := .

# Keep Python bytecode cache inside scripts/, not the project root
export PYTHONPYCACHEPREFIX := scripts/__pycache__

.PHONY: gui out install clean

# Default target: open the manifest manager
gui: .venv
	$(PYTHON) $(SCRIPT) $(BOOKDIR)

# Headless export to out/
out: .venv
	$(PYTHON) $(SCRIPT) $(BOOKDIR) --export

# Set up the virtual environment and install dependencies
install:
	python3 -m venv .venv
	.venv/bin/pip install -r scripts/requirements.txt
	chmod +x $(SCRIPT)

# Sentinel: remind the user to run make install if .venv is missing
.venv:
	@echo "Virtual environment not found. Run: make install"
	@exit 1

# Remove generated output
clean:
	rm -rf out/
