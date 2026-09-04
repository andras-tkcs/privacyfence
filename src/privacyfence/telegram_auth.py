"""Shared Telegram phone/code/2FA sign-in coroutines (P8, docs/https-
connector-refactor-plan.md §9.3's own words: "Telegram's phone + code + 2FA
flow is already a form in the settings UI and ports unchanged").

Extracted from settings_controller.py's telegram_start_auth/telegram_
submit_code/telegram_submit_2fa work() closures -- same telethon calls,
same short-lived connect/disconnect-per-step pattern (no long-lived
connection held across requests, since a browser round trip can be
arbitrarily far apart from the next one; see settings_controller.py's own
comment on this). Local mode calls these via ``asyncio.run()`` inside a
background thread (``_run_async``, unchanged); org mode's web/routes_
connect.py awaits them directly on the ASGI event loop its own route
handlers already run on -- there is no browser-loopback/redirect-URI
concept here at all (MTProto has no equivalent), so unlike Google/Slack/
Salesforce/Atlassian this is the *same* flow in both modes, just driven
from two different callers.
"""
from __future__ import annotations

NEEDS_2FA = "__needs_2fa__"


async def send_code(phone: str, session_file: str, api_id: int, api_hash: str) -> str:
    """Requests a Telegram sign-in code for ``phone``. Returns the
    ``phone_code_hash`` the caller must persist and pass to ``sign_in``."""
    from telethon import TelegramClient

    client = TelegramClient(session_file, api_id, api_hash)
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        return result.phone_code_hash
    finally:
        await client.disconnect()


async def sign_in(phone: str, code: str, phone_code_hash: str, session_file: str, api_id: int, api_hash: str) -> str:
    """Completes sign-in with the code sent by ``send_code``. Returns the
    signed-in account's display name, or ``NEEDS_2FA`` if the account also
    has two-step verification enabled (call ``sign_in_2fa`` next)."""
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    client = TelegramClient(session_file, api_id, api_hash)
    await client.connect()
    try:
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            return NEEDS_2FA
        me = await client.get_me()
        return f"{me.first_name or ''} {me.last_name or ''}".strip()
    finally:
        await client.disconnect()


async def sign_in_2fa(password: str, session_file: str, api_id: int, api_hash: str) -> str:
    """Completes sign-in with the account's two-step verification password,
    after ``sign_in`` returned ``NEEDS_2FA``. Returns the signed-in
    account's display name."""
    from telethon import TelegramClient

    client = TelegramClient(session_file, api_id, api_hash)
    await client.connect()
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        return f"{me.first_name or ''} {me.last_name or ''}".strip()
    finally:
        await client.disconnect()


__all__ = ["NEEDS_2FA", "send_code", "sign_in", "sign_in_2fa"]
