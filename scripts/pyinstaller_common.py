"""Shared PyInstaller ``Analysis`` inputs for every platform's app spec.

Extracted out of ``PrivacyFenceApp.spec`` (docs/windows-support-plan.md
Phase 2.1) so the datas/hidden-imports lists that describe *what the
daemon needs bundled* -- which has nothing to do with which platform is
doing the bundling -- live in exactly one place, imported by
``PrivacyFenceApp.spec`` (macOS) and ``PrivacyFenceApp.win.spec``
(Windows), and by whichever ``PrivacyFenceApp.linux.spec`` eventually
lands per ``docs/linux-local-deb-packaging-plan.md``. Without this, three
platform specs would carry three independently-drifting copies of the
same "don't forget openpyxl/telethon/the ten connectors" list.

Only the parts that are genuinely platform-independent live here: data
files and hidden imports. Everything platform-specific -- the icon
format, whether there's a ``BUNDLE()``/``.icns`` step, ``console=``,
target install layout -- stays in each spec file itself.

Usage from a spec file::

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent / "scripts"))
    from pyinstaller_common import DATAS, HIDDEN_IMPORTS

Spec files are exec'd by PyInstaller from the repo root, so the relative
``scripts/`` path above resolves the same way on every platform.
"""
from __future__ import annotations

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# ── data files ────────────────────────────────────────────────────────────
# Kept as a function, not a module-level constant: collect_data_files()/
# copy_metadata() walk the *build machine's* installed packages, so this
# must run at spec-exec time on whichever platform is actually building,
# not be frozen once and reused.


def collect_datas() -> list[tuple[str, str]]:
    return [
        # App icons and bundled resources
        ("src/privacyfence/resources", "privacyfence/resources"),
        # google-auth needs its transport files
        *collect_data_files("google"),
        *collect_data_files("googleapiclient"),
        # PyInstaller doesn't bundle a package's own .dist-info by default --
        # without this, src/privacyfence/__init__.py's
        # importlib.metadata.version("privacyfence") call would raise
        # PackageNotFoundError at runtime *inside the frozen app* (it works
        # fine at spec-exec time only because that runs unfrozen against the
        # build machine's installed package).
        *copy_metadata("privacyfence"),
    ]


# ── hidden imports ────────────────────────────────────────────────────────
# Modules loaded dynamically (importlib, __import__, a try/except
# ImportError fallback) that PyInstaller's static analysis can miss. This
# list genuinely doesn't vary by platform -- every connector ships in every
# platform's build -- so it's a plain constant, unlike DATAS above.

HIDDEN_IMPORTS: list[str] = [
    # google API discovery
    "googleapiclient.discovery",
    "googleapiclient.http",
    "google.auth.transport.requests",
    "google_auth_oauthlib.flow",
    # yaml
    "yaml",
    # slack
    "slack_sdk",
    "slack_sdk.web",
    "slack_sdk.errors",
    # salesforce (imported lazily inside a try/except ImportError, so
    # PyInstaller's static analysis needs an explicit nudge to bundle it)
    "simple_salesforce",
    # atlassian-python-api (Jira/Confluence) -- same defensive-listing pattern
    # as the other third-party clients above.
    "atlassian",
    # cryptography (google-auth dependency)
    "cryptography",
    # openpyxl (imported lazily inside a try/except ImportError by
    # audit_log.py's weekly Excel export, so needs the same explicit nudge)
    "openpyxl",
    # telethon (optional – Telegram; bundled so the connector works)
    "telethon",
    # portalocker (docs/windows-support-plan.md Phase 1): imported
    # unconditionally by daemon_main.py, but its Windows/POSIX backends are
    # selected dynamically at import time inside the package itself, which
    # is exactly the shape PyInstaller's static analysis can miss.
    "portalocker",
    # privacyfence connectors -- all ten, imported directly by daemon_main.py;
    # listed explicitly anyway as a defensive backstop against PyInstaller's
    # static analysis missing one.
    "privacyfence.connectors.gmail",
    "privacyfence.connectors.drive",
    "privacyfence.connectors.calendar",
    "privacyfence.connectors.contacts",
    "privacyfence.connectors.slack",
    "privacyfence.connectors.tasks",
    "privacyfence.connectors.telegram",
    "privacyfence.connectors.salesforce",
    "privacyfence.connectors.jira",
    "privacyfence.connectors.confluence",
]
