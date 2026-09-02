# Vetra — Smart Farming Animal Dx
#
# `make help` lists everything. The usual first run is:
#     make setup && make data && make train && make demo

PY      := .venv/bin/python
PIP     := .venv/bin/pip
PYTHON311 ?= python3.11

.DEFAULT_GOAL := help
.PHONY: help setup data train train-ml train-nn export evaluate test test-py test-js \
        server dashboard dashboard-build gateway simulate demo stop clean clean-data

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- setup ---------------------------------------------------------------
setup:  ## Create the venv and install Python + Node dependencies
	$(PYTHON311) -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	cd server && npm install
	cd dashboard && npm install
	@echo "\nSetup complete. Next: make data"

# --- machine learning ----------------------------------------------------
data:  ## Generate the synthetic herd and the windowed training set
	cd ml && ../$(PY) -m vetra_ml.make_dataset --animals 240 --days 5

train: train-ml train-nn export  ## Train everything and export the edge model

train-ml:  ## Train and compare the classical baselines
	cd ml && ../$(PY) -m vetra_ml.train_sklearn

train-nn:  ## Train the compact neural network
	cd ml && ../$(PY) -m vetra_ml.train_keras

export:  ## Convert to TFLite and pick the deployment variant
	cd ml && ../$(PY) -m vetra_ml.export_tflite

evaluate:  ## Measure the alert pipeline on held-out animals
	$(PY) scripts/evaluate_alerts.py

# --- tests ---------------------------------------------------------------
test: test-py test-js  ## Run every test suite

test-py:  ## Python tests
	$(PY) -m pytest tests/ -q

test-js:  ## Node.js API tests
	cd server && npm test

# --- running the system --------------------------------------------------
server:  ## Start the cloud service on :4000 (serves the built dashboard)
	node server/src/index.js

gateway:  ## Start the edge gateway on :5001
	$(PY) edge/agent.py --http-port 5001 --cloud-url http://localhost:4000

simulate:  ## Stream simulated collar data into the gateway
	$(PY) edge/simulator.py --transport rest --animals 12 --days 3 --speed 40000

dashboard:  ## Run the dashboard in development mode on :5173
	cd dashboard && npm run dev

dashboard-build:  ## Build the dashboard for the cloud service to serve
	cd dashboard && npm run build

demo:  ## Start the whole system and stream a herd through it
	./scripts/run_demo.sh

stop:  ## Stop anything the demo left running
	-@pkill -f "node server/src/index.js" 2>/dev/null || true
	-@pkill -f "\.venv/bin/python edge/agent.py" 2>/dev/null || true
	@echo "stopped"

# --- housekeeping --------------------------------------------------------
clean:  ## Remove local databases and Python caches
	rm -f edge/*.db edge/*.db-* server/*.db server/*.db-*
	find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name .pytest_cache -type d -exec rm -rf {} + 2>/dev/null || true

clean-data:  ## Also remove generated datasets and trained models
	rm -rf data/raw/* data/processed/* artifacts/models/* artifacts/reports/*
	@touch data/raw/.gitkeep data/processed/.gitkeep artifacts/models/.gitkeep artifacts/reports/.gitkeep
