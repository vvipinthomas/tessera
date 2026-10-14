# Common tasks. Run `make help`.
.PHONY: help dev install test test-fast ui lint security check docker docs landing clean

help:            ## show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t 16

install:         ## install Tessera with development extras
	pip install -e ".[dev,mysql,s3,mongodb,delta]"

dev:             ## run a development server on http://127.0.0.1:8710 with data in ./dev-data
	TESSERA_DATA_DIR=./dev-data tessera serve

test:            ## run the full test suite
	pytest -q

test-fast:       ## run tests except the slow browser and streaming ones
	pytest -q --ignore=tests/test_streaming.py

ui:              ## drive every screen in Chromium
	python tests/ui_smoke.py

lint:            ## lint Python
	ruff check tessera tests scripts

security:        ## security lint and dependency audit
	bandit -q -r tessera -c pyproject.toml
	pip-audit

check: lint security test   ## everything CI checks

docker:          ## build the container image
	docker build -t tessera:dev .

docs:            ## serve the documentation site locally
	pip install "mkdocs>=1.6,<2" "mkdocs-material>=9.5,<10" && mkdocs serve

landing:         ## build the standalone landing page into site/index.html
	mkdir -p site && python scripts/build_artifact.py site/index.html

clean:           ## remove build artefacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache site
