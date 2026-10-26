.PHONY: init install

init:
	@echo "Initializing git submodules..."
	git submodule update --init --recursive

install:
	@echo "Installing dependencies..."
	uv sync
	@echo "Dependencies installed."
	@echo "Activate the environment with: source .venv/bin/activate"
