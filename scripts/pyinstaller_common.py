# PyInstaller `datas`/`hidden_imports` shared between PrivacyFenceApp.spec (macOS),
# PrivacyFenceApp.linux.spec (Linux, the now-removed linux-local-deb-packaging-plan.md Phase 1), and
# PrivacyFenceApp.win.spec (Windows, the now-removed windows-support-plan.md Phase 2.1). All three specs
# build the same daemon entry point (src/_daemon_entry.py) against the same dependency set --
# the only things that differ between platforms are the packaging step around the PyInstaller
# output (BUNDLE() + .icns + codesign on macOS; a bare onedir + `debian/` packaging on Linux; a
# bare onedir + .ico + Inno Setup on Windows), not what goes into the frozen daemon itself.
# Factored out here instead of duplicated in three spec files so they can't quietly drift (a
# hidden import added for one platform but not the others, discovered only when that platform's
# build breaks).
#
# Every spec imports this the same way:
#
#   import sys
#   from pathlib import Path
#   sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
#   from pyinstaller_common import DATAS, HIDDEN_IMPORTS
#
# (PyInstaller execs spec files without the repo root's scripts/ on sys.path by default, hence the
# explicit insert rather than a plain top-level import.)

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


# Data files bundled into the frozen app, identical on every platform.
DATAS = [
    # App icons and bundled resources
    ("src/privacyfence/resources", "privacyfence/resources"),
    # google-auth needs its transport files
    *collect_data_files("google"),
    *collect_data_files("googleapiclient"),
    # PyInstaller doesn't bundle a package's own .dist-info by default --
    # without this, src/privacyfence/__init__.py's
    # importlib.metadata.version("privacyfence") call would raise
    # PackageNotFoundError at runtime *inside the frozen app* (it worked fine
    # a moment ago in the spec file, above, only because that ran unfrozen
    # against the build machine's installed package).
    *copy_metadata("privacyfence"),
]


# Modules loaded dynamically (importlib, __import__) that PyInstaller can miss.
HIDDEN_IMPORTS = [
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
    # portalocker (the now-removed windows-support-plan.md Phase 1): imported
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
