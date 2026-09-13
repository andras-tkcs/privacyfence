"""Tests for download_staging.py: the encrypted-at-rest staging store
behind org-mode download delivery."""
from __future__ import annotations

import pytest

from privacyfence import download_staging, paths
from privacyfence.download_staging import DownloadStagingStore
from privacyfence.principal import Principal

ALICE = Principal(id="alice", email="alice@example.com")
BOB = Principal(id="bob", email="bob@example.com")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


class TestStageAndClaim:
    def test_round_trip_returns_the_right_bytes_name_and_mime_type(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"hello world", "report.pdf", "application/pdf")
        result = store.claim(token, ALICE.id)
        assert result == (b"hello world", "report.pdf", "application/pdf")

    def test_ciphertext_file_is_gone_after_claim(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"secret payload", "file.bin", "application/octet-stream")
        downloads = list(paths.downloads_dir(ALICE).iterdir())
        assert len(downloads) == 1
        store.claim(token, ALICE.id)
        assert list(paths.downloads_dir(ALICE).iterdir()) == []

    def test_second_claim_of_the_same_token_fails(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        assert store.claim(token, ALICE.id) is not None
        assert store.claim(token, ALICE.id) is None

    def test_the_file_on_disk_is_never_plaintext(self):
        """The one test that actually proves the encryption-at-rest design
        works, not just that the API round-trips -- see download_staging.py's
        own module docstring."""
        store = DownloadStagingStore()
        plaintext = b"This is a strictly confidential document. " * 50
        store.stage(ALICE, plaintext, "confidential.txt", "text/plain")
        [on_disk] = list(paths.downloads_dir(ALICE).iterdir())
        raw = on_disk.read_bytes()
        assert raw != plaintext
        assert plaintext not in raw

    def test_name_is_sanitized_to_a_basename(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"x", "../../etc/passwd", "text/plain")
        _, name, _ = store.claim(token, ALICE.id)
        assert name == "passwd"

    def test_default_mime_type_when_empty(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"x", "f", "")
        _, _, mime_type = store.claim(token, ALICE.id)
        assert mime_type == "application/octet-stream"


class TestClaimFailureCases:
    def test_unknown_token_returns_none(self):
        store = DownloadStagingStore()
        store.stage(ALICE, b"data", "f.txt", "text/plain")
        forged_token = b"\x00" * 32
        assert store.claim(forged_token, ALICE.id) is None

    def test_expired_token_returns_none(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain", ttl_seconds=-1.0)
        assert store.claim(token, ALICE.id) is None

    def test_expired_token_sweeps_the_orphaned_ciphertext_file(self):
        store = DownloadStagingStore()
        store.stage(ALICE, b"data", "f.txt", "text/plain", ttl_seconds=-1.0)
        # A later stage()/claim() call opportunistically sweeps expired
        # entries -- trigger that via a second stage() for a different file.
        store.stage(ALICE, b"other", "g.txt", "text/plain", ttl_seconds=300.0)
        remaining = list(paths.downloads_dir(ALICE).iterdir())
        assert len(remaining) == 1  # only the still-live second file

    def test_wrong_principal_returns_none_even_with_the_correct_token(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        assert store.claim(token, BOB.id) is None
        # Still claimable by the right principal afterward -- a failed
        # cross-principal claim must not consume it.
        assert store.claim(token, ALICE.id) is not None

    def test_no_oracle_between_wrong_token_and_expired_token(self):
        # Both a token that never existed and one that existed but expired
        # return exactly None -- proving there's no distinguishing signal.
        store = DownloadStagingStore()
        expired_token = store.stage(ALICE, b"data", "f.txt", "text/plain", ttl_seconds=-1.0)
        forged_token = b"\x11" * 32
        assert store.claim(expired_token, ALICE.id) is store.claim(forged_token, ALICE.id) is None


class TestClaimDiskFailures:
    def test_ciphertext_missing_on_disk_returns_none(self):
        """A live registry entry whose file vanished out-of-band (manual
        cleanup, a filesystem issue) fails closed rather than raising."""
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        [on_disk] = list(paths.downloads_dir(ALICE).iterdir())
        on_disk.unlink()

        assert store.claim(token, ALICE.id) is None

    def test_corrupted_ciphertext_fails_decrypt_and_returns_none(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        [on_disk] = list(paths.downloads_dir(ALICE).iterdir())
        on_disk.write_bytes(b"\x00" * 40)  # same rough shape, wrong content

        assert store.claim(token, ALICE.id) is None

    def test_claim_tolerates_a_cleanup_unlink_failure_and_still_returns_the_file(self, monkeypatch):
        """The ciphertext-delete-on-claim in the `finally` block is best-
        effort cleanup, not load-bearing for correctness -- a permission
        error there must not stop the human from getting their file."""
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")

        from pathlib import Path
        real_unlink = Path.unlink
        monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("locked")))
        try:
            result = store.claim(token, ALICE.id)
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)
        assert result == (b"data", "f.txt", "text/plain")

    def test_sweep_tolerates_an_unlink_failure_on_an_expired_entry(self, monkeypatch):
        store = DownloadStagingStore()
        store.stage(ALICE, b"data", "f.txt", "text/plain", ttl_seconds=-1.0)

        from pathlib import Path
        real_unlink = Path.unlink

        def failing_unlink(self, *a, **k):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        try:
            # Must not raise -- the sweep logs and moves on.
            store.stage(ALICE, b"other", "g.txt", "text/plain", ttl_seconds=300.0)
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)


class TestStartupSweep:
    """DownloadStagingStore.__init__ must not let a daemon restart turn a
    lost registry entry into ciphertext that lingers forever -- see the
    module docstring's "Restart is not supposed to leak ciphertext"
    section. Each test writes the leftover file directly, simulating what
    a *previous* store instance left behind, rather than going through
    stage() on the same instance -- that's the one thing this sweep must
    never touch (see test_does_not_touch_files_staged_after_construction
    below)."""

    def test_removes_a_ciphertext_file_left_by_a_previous_process(self):
        downloads = paths.downloads_dir(ALICE)
        leftover = downloads / "orphaned-lookup-id"
        leftover.write_bytes(b"some old ciphertext")

        DownloadStagingStore()

        assert not leftover.exists()

    def test_sweeps_every_principals_downloads_dir_not_just_one(self):
        alice_leftover = paths.downloads_dir(ALICE) / "a"
        alice_leftover.write_bytes(b"x")
        bob_leftover = paths.downloads_dir(BOB) / "b"
        bob_leftover.write_bytes(b"y")

        DownloadStagingStore()

        assert not alice_leftover.exists()
        assert not bob_leftover.exists()

    def test_does_not_touch_files_staged_after_construction(self):
        store = DownloadStagingStore()
        token = store.stage(ALICE, b"fresh data", "f.txt", "text/plain")

        assert store.claim(token, ALICE.id) == (b"fresh data", "f.txt", "text/plain")

    def test_a_fresh_store_with_nothing_on_disk_constructs_cleanly(self):
        # No downloads/ or users/ directory exists at all yet -- must not
        # raise just because there's nothing to sweep.
        DownloadStagingStore()

    def test_tolerates_an_unlink_failure_during_startup_sweep(self, monkeypatch):
        leftover = paths.downloads_dir(ALICE) / "orphaned-lookup-id"
        leftover.write_bytes(b"some old ciphertext")

        from pathlib import Path
        real_unlink = Path.unlink

        def failing_unlink(self, *a, **k):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        try:
            # Must not raise -- a startup cleanup failure logs and moves
            # on rather than stopping the daemon from starting at all.
            DownloadStagingStore()
        finally:
            monkeypatch.setattr(Path, "unlink", real_unlink)


class TestSingleton:
    def test_get_download_staging_store_is_lazily_constructed_and_stable(self):
        download_staging._INSTANCE = None
        first = download_staging.get_download_staging_store()
        second = download_staging.get_download_staging_store()
        assert first is second
        download_staging._INSTANCE = None
