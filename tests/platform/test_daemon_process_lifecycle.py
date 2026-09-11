"""The real daemon, started and stopped as a genuinely separate OS process
(docs/automated-test-strategy-plan.md Phase 2.3 -- "process spawning,"
"daemon startup," "shim daemon discovery (mcp_url file)," and "process
cleanup on shutdown").

Every other daemon-startup test in this repo drives ``daemon_main.run_app()``
or ``main()`` as a plain Python call inside the *test's own process* (see
tests/unit/test_daemon_main.py) -- real behavior, but never across an actual
process boundary, so nothing proves the one thing every real install
depends on: that ``python -m privacyfence.daemon_main`` (what the packaged
app, a systemd unit, and mcpb/shim/src/daemon.ts's own spawn call all
eventually run) actually comes up as its own process, binds a real socket,
writes the ``mcp_url`` file the shim discovers it through (protocol.ts),
and goes away cleanly when killed -- freeing both the port and the
single-instance lock for whatever starts next. This is deliberately a small
slice of that, not the full daemon/MCP/approval/audit contract --
docs/automated-test-strategy-plan.md's own Phase 3 (``tests/system/
test_local_mode_system.py``, not yet built) owns that larger scenario, on
all three OSes, reusing patterns from this module and from
tests/integration/test_mcp_daemon_contract.py rather than duplicating them
here.

Isolation without a real package install
-----------------------------------------
``paths.data_dir()`` resolves to the repo checkout root itself in dev/
editable-install mode (see paths.py's own docstring) -- spawning the real
entry point unmodified would read/write *this* checkout's own state.
tests/integration/test_org_ubuntu_release_smoke.py solves this with a real
``pip install --target`` into an isolated site-packages directory (needed
there since it's proving a genuine non-editable-install deployment shape);
that's real but relatively heavy machinery this module doesn't need, since
all it has to prove is that the entry point behaves correctly as a process,
not that installation itself works. Instead, the spawned process's own
bootstrap (a `-c` script, not `-m`, so it runs *before*
``privacyfence.daemon_main`` is imported) sets ``sys.frozen``/
``sys._MEIPASS`` -- the same two attributes ``paths.is_bundled()`` checks --
which flips ``data_dir()`` onto its ``Path.home() / ".privacyfence"``
branch, same as a real packaged .app. Pointing ``$HOME``/``%USERPROFILE%``
(both -- ``Path.home()`` reads different env vars per platform, see
docs/windows-support-plan.md 6.3's own "set both" fix for exactly this) at
a fresh ``tmp_path`` then sandboxes everything the daemon touches.
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

import privacyfence

pytestmark = pytest.mark.platform

# Runs *before* `from privacyfence import daemon_main` -- daemon_main.py
# computes PROJECT_ROOT (== data_dir()) as a module-level constant at
# import time, so is_bundled() must already read True by then.
_BOOTSTRAP = """
import sys
sys.frozen = True
sys._MEIPASS = "platform-test-bundle"
from privacyfence import daemon_main
sys.exit(daemon_main.main([]))
"""


def _free_port() -> int:
    """A real, currently-unused TCP port -- WebServer.start() doesn't report
    back the OS-assigned port for ``port=0`` (its ``mcp_url`` property builds
    the URL from the *configured* port, not the bound socket -- see
    web/server.py), so this needs a real port number up front, same as
    tests/integration/test_mcp_daemon_contract.py's own ``_free_port()``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare_home(tmp_path: Path, *, port: int) -> Path:
    """A fresh $HOME with a pre-seeded settings.yaml -- a real free port
    (see ``_free_port()`` above) and update_check disabled (this tier makes
    no real outbound network calls, even ones the daemon itself treats as
    best-effort/silent-on-failure -- see settings.yaml.example's own
    update_check comment)."""
    home = tmp_path / "home"
    home.mkdir()
    example = Path(privacyfence.__file__).parent / "resources" / "settings.yaml.example"
    config = yaml.safe_load(example.read_text(encoding="utf-8"))
    config["web"]["port"] = port
    config["update_check"]["enabled"] = False
    config_dir = home / ".privacyfence" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return home


@contextlib.contextmanager
def _daemon(home: Path):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Path.home() on Windows reads this, not $HOME
    log_path = home / "daemon.log"
    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-c", _BOOTSTRAP],
            env=env, cwd=str(home), stdout=log_fh, stderr=subprocess.STDOUT,
        )
        try:
            yield proc, log_path
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _wait_for_mcp_url(proc: subprocess.Popen, home: Path, log_path: Path, timeout: float = 20.0) -> str:
    mcp_url_file = home / ".privacyfence" / "mcp_url"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited early (code {proc.returncode}) instead of starting -- log:\n"
                f"{log_path.read_text(errors='replace')}"
            )
        if mcp_url_file.exists():
            content = mcp_url_file.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.1)
    raise AssertionError(
        f"mcp_url discovery file never appeared under {timeout}s -- log:\n{log_path.read_text(errors='replace')}"
    )


def _can_connect(host: str, port: int) -> bool:
    with contextlib.suppress(OSError):
        with socket.create_connection((host, port), timeout=1.0):
            return True
    return False


def _wait_until_connectable(host: str, port: int, timeout: float = 10.0) -> None:
    """The ``mcp_url`` file is written right after ``WebServer.start()``
    spawns its own thread (see web/server.py) -- not after uvicorn has
    actually finished binding and entered its accept loop inside that
    thread, which can lag the file by a few milliseconds. A single
    ``_can_connect`` right after the file appears can therefore race a
    listener that's a moment away from being ready, not actually absent."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _can_connect(host, port):
            return
        time.sleep(0.05)
    raise AssertionError(f"nothing accepted a connection on {host}:{port} within {timeout}s of mcp_url appearing")


def test_daemon_starts_as_a_real_process_and_is_discoverable_via_mcp_url(tmp_path):
    port = _free_port()
    home = _prepare_home(tmp_path, port=port)
    with _daemon(home) as (proc, log_path):
        mcp_url = _wait_for_mcp_url(proc, home, log_path)

        parsed = urlparse(mcp_url)
        assert parsed.scheme == "http"
        assert parsed.path == "/mcp"
        assert parsed.port == port, f"mcp_url did not carry the configured port {port}: {mcp_url}"

        # The exact thing mcpb/shim/src/daemon.ts does with this file
        # (protocol.ts's MCP_URL_FILE): discover the host:port and confirm
        # something real is actually listening there, in a real OS process.
        _wait_until_connectable(parsed.hostname, parsed.port)

        # This test's own daemon.log confirms it's really the subprocess
        # (not some leftover listener) -- a nonzero exit would already have
        # raised inside _wait_for_mcp_url above.
        assert proc.poll() is None

        lock_file = home / ".privacyfence" / "privacyfence.lock"
        assert lock_file.exists()


def test_terminate_frees_the_port_and_lock_for_a_fresh_instance(tmp_path):
    port = _free_port()
    home = _prepare_home(tmp_path, port=port)
    with _daemon(home) as (proc, log_path):
        _wait_for_mcp_url(proc, home, log_path)

        proc.terminate()
        exit_code = proc.wait(timeout=15)
        assert exit_code is not None

    # No lingering listener from the dead process.
    assert not _can_connect("127.0.0.1", port)

    # A second instance against the same $HOME starts cleanly right after --
    # proves the instance lock (daemon_main._acquire_instance_lock) and any
    # state the first process left behind don't wedge the next launch, the
    # same property a real crash-and-restart depends on.
    with _daemon(home) as (proc2, log_path2):
        second_mcp_url = _wait_for_mcp_url(proc2, home, log_path2)
        assert urlparse(second_mcp_url).port == port
        _wait_until_connectable("127.0.0.1", port)
