"""PrivacyFence: privacy proxy between Claude (MCP) and your personal data, with an embedded web
approval/settings UI."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("privacyfence")
except PackageNotFoundError:
    # Not installed with metadata at all -- e.g. run straight out of a
    # source checkout without `pip install -e .`/`pip install .` first, or a
    # frozen build missing --copy-metadata (see scripts/build_dmg.sh). Never
    # crash the whole package over a display string: fall back to the same
    # placeholder [tool.setuptools_scm]'s fallback_version in pyproject.toml
    # uses for "no tag reachable yet", so update_checker.py's parse_version()
    # still accepts it (and correctly never claims it's up to date).
    __version__ = "0.0.0.dev0"
