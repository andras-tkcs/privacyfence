# Recorded live fixtures

Populated by `scripts/qa_fixture_recorder.py --record <connector>`, run locally against a real,
already-authenticated account per [`qa-environment-setup.md`](../../../docs/qa-environment-setup.md).
Never generated in CI, never containing anything but a `[QATEST]`-tagged seed artifact with
identity fields already redacted — see
[`external-api-contract-testing.md`](../../../docs/external-api-contract-testing.md).

Loaded by `tests/unit/test_<connector>_client.py`'s `TestLiveFixtureParsing` classes, which replay
these files through the real `_parse_*` methods on every PR. If a fixture is missing for a given
connector, that connector's `TestLiveFixtureParsing` tests are skipped (not failed) with a message
pointing back at the recorder — nothing here is required for the suite to pass, but a connector
without a recorded fixture doesn't get this layer's regression coverage either.

Re-record after a genuine provider API change, not routinely — see the "Local checks before
opening a PR" section of `external-api-contract-testing.md`.
