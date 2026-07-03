"""PrivacyFence application-level credentials.

Telegram's api_id/api_hash identify the PrivacyFence *application* to
Telegram's API (MTProto has no concept of "organization" the way OAuth
does) — they are the same for every user and every phone number, and are
not a substitute for per-user auth (still phone + code + optional 2FA, see
menu_bar.py). Because this repo is public, the real values are never
committed: release builds bake them in from CI secrets (see
scripts/build_dmg.sh) into the git-ignored ``_telegram_credentials`` module
generated right before packaging. Local/dev builds fall back to the
PRIVACYFENCE_TELEGRAM_API_ID / PRIVACYFENCE_TELEGRAM_API_HASH env vars.
"""
from __future__ import annotations

import os


def telegram_app_credentials() -> tuple[int, str] | None:
    try:
        from . import _telegram_credentials  # generated at build time; git-ignored
    except ImportError:
        pass
    else:
        return int(_telegram_credentials.API_ID), _telegram_credentials.API_HASH

    api_id = os.environ.get("PRIVACYFENCE_TELEGRAM_API_ID")
    api_hash = os.environ.get("PRIVACYFENCE_TELEGRAM_API_HASH")
    if api_id and api_hash:
        return int(api_id), api_hash
    return None
