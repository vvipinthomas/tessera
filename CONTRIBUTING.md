# Contributing to Tessera

Thanks for helping. Tessera aims to be the data platform a small team can run themselves and trust, so contributions
are judged on three things: does it work, is it tested, and does it keep people's data safe.

## Ways to help

- **Report a bug** with the [bug template](https://github.com/vvipinthomas/tessera/issues/new?template=bug_report.yml). Include the version (`tessera version`), what you did, what you expected and what happened.
- **Suggest a feature** with the [feature template](https://github.com/vvipinthomas/tessera/issues/new?template=feature_request.yml). Describe the problem first; the solution can come later.
- **Pick up an issue** labelled [`good first issue`](https://github.com/vvipinthomas/tessera/labels/good%20first%20issue) or [`help wanted`](https://github.com/vvipinthomas/tessera/labels/help%20wanted). Comment on it so others know.
- **Improve the docs.** If something confused you, it will confuse the next person.
- **Report a security problem privately**, never in an issue. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/vvipinthomas/tessera.git && cd tessera
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mysql,s3,mongodb,delta]"
python -m playwright install chromium        # only for the browser test
TESSERA_DATA_DIR=./dev-data tessera serve    # http://127.0.0.1:8710
```

The front end is plain JavaScript in `tessera/static/` with no build step: edit a file and reload the browser.

Or `make dev`, `make test`, `make lint`, `make ui` (see the [Makefile](Makefile)).

## Tests

```bash
pytest                                     # the full suite, about 4 minutes
pytest tests/test_streaming.py -q          # one area
python tests/ui_smoke.py                   # drives every screen in Chromium, light and dark, desktop and mobile
ruff check tessera tests scripts           # lint
bandit -r tessera -c pyproject.toml        # security lint
pip-audit                                  # known vulnerabilities in dependencies
```

Some tests use real services and skip themselves when they are not available:

| Test | Needs | How |
|---|---|---|
| PostgreSQL connector and delivery | A PostgreSQL server | `TESSERA_TEST_PG="host=127.0.0.1 port=5432 user=postgres dbname=postgres" pytest` |
| Kafka streams | A Kafka-protocol test broker | `sh tests/kafka_broker/build.sh` (needs Go 1.24+) |
| MQTT, Kinesis, SFTP, Iceberg, Delta, S3, MongoDB | Python packages from the `dev` extra | Installed by `pip install -e ".[dev]"` |

CI runs all of them.

## Pull requests

1. Fork, create a branch from `main` (`fix/stream-lag`, `feat/pulsar-connector`).
2. Keep the change focused. One feature or fix per pull request.
3. **Add or update tests.** New connectors need a test against the real protocol or a faithful stand-in, like the
   existing ones. New routes are covered by the authorization matrix automatically; make sure they refuse anonymous
   and under-privileged callers.
4. Update the docs (`docs/`, `README.md`) and add a line to `CHANGELOG.md` under *Unreleased*.
5. `make check` must pass (lint, security lint, tests).
6. Open the pull request and fill in the template. A maintainer will review within a week.

By contributing you agree that your contribution is licensed under the Apache License 2.0, the project's licence.
Sign off your commits (`git commit -s`) to certify the [Developer Certificate of Origin](https://developercertificate.org).

## Design rules worth knowing

- **Security first.** User SQL goes through `Warehouse.validate_select`. Never build SQL from user values; quote
  identifiers with `qi()` and bind values as parameters. Restricted users only ever see `serving`.
- **Honest product copy.** Do not claim a connector or feature is tested unless the suite tests it. Connectors that
  are not exercised by tests are marked *driver*.
- **Errors tell people what to do.** "Topic 'orders' was not found or has no partitions" beats "Error 3".
- **No new front-end dependencies** without discussion; the app ships with a strict Content Security Policy.
- **Plain language in the interface:** sentence case, active voice, no jargon where a plain word works.

## Adding a connector

1. Add the type to `CONNECTOR_TYPES` (or a module like `connectors_lake.py`) with its fields; mark secrets with
   `secret=True`.
2. Implement `test()`, `browse()` and `read(source, watermark)` returning a `pyarrow.Table`.
3. Guard any network address with `guard_url` and any file path with `_within`.
4. Add an optional extra in `pyproject.toml` for its driver.
5. Write tests in `tests/` and add the connector to the docs.

## Release process (maintainers)

1. Update `CHANGELOG.md`, bump `version` in `pyproject.toml` and `tessera/__init__.py`.
2. `make check && python tests/ui_smoke.py`.
3. Tag `vX.Y.Z` and push. The release workflow builds the package and the multi-architecture container image, and
   publishes the GitHub release.
