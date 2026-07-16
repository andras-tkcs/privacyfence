# Recorded live fixtures

Populated by `scripts/qa_fixture_recorder.py --record <connector>`, run locally against a real,
already-authenticated account per [`qa-environment-setup.md`](../../../docs/qa-environment-setup.md).
Never generated in CI, never containing anything but a `[QATEST]`-tagged seed artifact with
identity fields and structural (non-identity) resource ids/URLs already de-identified — see the
`redact()`/`deidentify_structural_fields()` functions and their connector-specific passes in
`scripts/qa_fixture_recorder.py`.

Loaded by `tests/unit/test_<connector>_client.py`'s `TestLiveFixtureParsing` classes, which replay
these files through the real `_parse_*` methods on every PR. If a fixture is missing for a given
connector, that connector's `TestLiveFixtureParsing` tests are skipped (not failed) with a message
pointing back at the recorder — nothing here is required for the suite to pass, but a connector
without a recorded fixture doesn't get this layer's regression coverage either.

Re-record after a genuine provider API change, not routinely — see
[`testing-policy.md` §2.1](../../../docs/testing-policy.md#21-qa_fixture_recorderpy---check---record).
