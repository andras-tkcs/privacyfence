"""Tests for secure_files.py (SEC-09, docs/security-remediation-plan.md
Phase 1 item 1.4): the shared atomic-write and directory-permission helpers
every credential/token/config writer in this codebase now routes through.

The two invariants that matter most, and that most of the tests below exist
to pin down: a reader can never observe a partially-written file (atomic
replace), and a written file/directory is never left more permissive than
its final ``mode`` -- even when the umask, an already-existing directory, or
a failed ``chmod`` would otherwise have let that happen.
"""
from __future__ import annotations

import json
import stat

import pytest

from privacyfence import secure_files


class TestSecureMkdir:
    def test_creates_missing_directory_at_0700(self, tmp_path):
        target = tmp_path / "a" / "b"

        result = secure_files.secure_mkdir(target)

        assert result == target
        assert target.is_dir()
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_re_tightens_an_existing_directory(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        target.chmod(0o755)

        secure_files.secure_mkdir(target)

        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_custom_mode(self, tmp_path):
        target = tmp_path / "custom"

        secure_files.secure_mkdir(target, mode=0o750)

        assert stat.S_IMODE(target.stat().st_mode) == 0o750

    def test_chmod_failure_is_non_fatal(self, tmp_path, monkeypatch):
        target = tmp_path / "unchmoddable"
        monkeypatch.setattr("os.chmod", lambda *a, **kw: (_ for _ in ()).throw(OSError("no chmod")))

        result = secure_files.secure_mkdir(target)  # must not raise

        assert result.is_dir()


class TestAtomicWriteBytes:
    def test_writes_content_and_default_permissions(self, tmp_path):
        target = tmp_path / "nested" / "file.bin"

        secure_files.atomic_write_bytes(target, b"hello")

        assert target.read_bytes() == b"hello"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_creates_parent_directory_at_0700(self, tmp_path):
        target = tmp_path / "nested" / "file.bin"

        secure_files.atomic_write_bytes(target, b"hello")

        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700

    def test_custom_mode(self, tmp_path):
        target = tmp_path / "file.bin"

        secure_files.atomic_write_bytes(target, b"hello", mode=0o644)

        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_overwrites_existing_file_completely(self, tmp_path):
        target = tmp_path / "file.bin"
        target.write_bytes(b"old content, longer than the new one")

        secure_files.atomic_write_bytes(target, b"new")

        assert target.read_bytes() == b"new"

    def test_leaves_no_temp_file_behind_on_success(self, tmp_path):
        target = tmp_path / "file.bin"

        secure_files.atomic_write_bytes(target, b"hello")

        assert list(tmp_path.iterdir()) == [target]

    def test_original_file_untouched_and_no_temp_left_behind_on_write_failure(self, tmp_path, monkeypatch):
        """A failure after the temp file is fully written (simulated here
        via a failing fsync, which is exactly where a real disk-full/IO
        error would surface) must never touch the real destination -- the
        whole point of writing-to-temp-then-replace."""
        target = tmp_path / "file.bin"
        target.write_bytes(b"original")
        monkeypatch.setattr("os.fsync", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

        with pytest.raises(OSError):
            secure_files.atomic_write_bytes(target, b"new content")

        # A reader never sees the partial write -- the old file is intact,
        # and the failed temp file was cleaned up rather than left as
        # debris in the credentials directory.
        assert target.read_bytes() == b"original"
        assert list(tmp_path.iterdir()) == [target]

    def test_chmod_failure_on_temp_file_is_non_fatal(self, tmp_path, monkeypatch):
        target = tmp_path / "file.bin"
        monkeypatch.setattr("os.chmod", lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only filesystem")))

        secure_files.atomic_write_bytes(target, b"hello")  # must not raise

        assert target.read_bytes() == b"hello"

    def test_cleanup_failure_after_a_write_failure_does_not_mask_the_original_error(self, tmp_path, monkeypatch):
        """A double failure -- the write itself fails, and the best-effort
        temp-file cleanup that follows also fails -- must still surface the
        original error, not a cleanup-time one."""
        target = tmp_path / "file.bin"
        monkeypatch.setattr("os.fsync", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
        monkeypatch.setattr(
            "pathlib.Path.unlink", lambda *a, **kw: (_ for _ in ()).throw(OSError("cleanup also failed")),
        )

        with pytest.raises(OSError, match="disk full"):
            secure_files.atomic_write_bytes(target, b"new content")


class TestAtomicWriteText:
    def test_writes_text_with_default_encoding(self, tmp_path):
        target = tmp_path / "file.txt"

        secure_files.atomic_write_text(target, "héllo")

        assert target.read_text(encoding="utf-8") == "héllo"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


class TestAtomicWriteJson:
    def test_writes_valid_json_round_tripping_the_input(self, tmp_path):
        target = tmp_path / "file.json"
        data = {"token": "abc", "nested": {"a": 1}}

        secure_files.atomic_write_json(target, data)

        assert json.loads(target.read_text(encoding="utf-8")) == data
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_forwards_json_dumps_kwargs(self, tmp_path):
        target = tmp_path / "file.json"

        secure_files.atomic_write_json(target, {"b": 1, "a": 2}, indent=2, sort_keys=True)

        text = target.read_text(encoding="utf-8")
        assert text == json.dumps({"b": 1, "a": 2}, indent=2, sort_keys=True)


class TestAuditDirectoryPermissions:
    def test_no_problems_for_a_0700_directory(self, tmp_path):
        target = tmp_path / "secure"
        target.mkdir()
        target.chmod(0o700)

        assert secure_files.audit_directory_permissions([target]) == []

    def test_stricter_than_0700_is_also_fine(self, tmp_path):
        target = tmp_path / "locked"
        target.mkdir()
        target.chmod(0o500)

        assert secure_files.audit_directory_permissions([target]) == []

    @pytest.mark.parametrize("mode", [0o750, 0o705, 0o777, 0o755])
    def test_flags_group_or_other_access(self, tmp_path, mode):
        target = tmp_path / "loose"
        target.mkdir()
        target.chmod(mode)

        problems = secure_files.audit_directory_permissions([target])

        assert len(problems) == 1
        assert str(target) in problems[0]
        assert f"{mode:04o}" in problems[0]

    def test_skips_a_directory_that_does_not_exist(self, tmp_path):
        missing = tmp_path / "does-not-exist"

        assert secure_files.audit_directory_permissions([missing]) == []

    def test_skips_a_path_that_is_a_file_not_a_directory(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("x")
        target.chmod(0o777)

        assert secure_files.audit_directory_permissions([target]) == []

    def test_checks_every_directory_given_independently(self, tmp_path):
        loose = tmp_path / "loose"
        loose.mkdir()
        loose.chmod(0o755)
        tight = tmp_path / "tight"
        tight.mkdir()
        tight.chmod(0o700)

        problems = secure_files.audit_directory_permissions([loose, tight])

        assert len(problems) == 1
        assert str(loose) in problems[0]
