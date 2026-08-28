#!/usr/bin/env python3
"""Slack API call budget for PrivacyFence's gated tools.

Counts how many Slack Web API calls each ``SlackClient`` entry point makes
for one bridge-tool invocation, then turns that count into a wall-clock
estimate using Slack's per-method rate-limit tiers. Everything a gated read
does here happens *before* ``gated_call`` shows the approval popup, so this
number is, directly, how long the user waits before the popup appears --
and how far past the calling MCP client's request timeout that lands.

Offline: a fake WebClient records every call and returns plausible
fixtures. Run with no arguments:

    .venv/bin/python scripts/slack_call_budget.py

Workspace shape is a mid-size company (2000 members, 900 channels, 600 `im`
conversations, 40 group DMs); override with --users/--channels/--ims/--mpims.
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from privacyfence import slack_client as sc  # noqa: E402

# docs.slack.dev/apis/web-api/rate-limits: T1 1/min, T2 20/min, T3 50/min,
# T4 100/min. conversations.history/.replies are T3 for internal and
# Marketplace apps, but 1/min with a hard 15-object cap for apps distributed
# outside the Marketplace (2025-05-29 changelog, enforced on existing
# installs from 2025-09-02) -- hence --distributed below.
TIER_PER_MINUTE = {
    "conversations.list": 20,
    "conversations.members": 100,
    "conversations.info": 50,
    "conversations.history": 50,
    "conversations.replies": 50,
    "users.info": 100,
    "users.list": 20,
    "search.messages": 20,
    "users.conversations": 100,  # matches conversations.members' tier
}
# Round trip to slack.com. The sync WebClient builds a fresh urllib opener
# per call (no connection pooling), so each of these also pays a TLS
# handshake -- the low end of this range is optimistic, not typical.
RTT_SECONDS = 0.25

calls: collections.Counter = collections.Counter()


class Resp(dict):
    @property
    def data(self):
        return self


def _page(items, key, cursor, size):
    start = int(cursor or 0)
    nxt = str(start + size) if start + size < len(items) else ""
    return Resp({key: items[start:start + size], "response_metadata": {"next_cursor": nxt}})


def _channel_members(channel_id: str, n_users: int) -> list[str]:
    """Deterministic synthetic membership (6 users) for one channel/group
    chat -- varies by channel so conversations_members (the per-item fake)
    and users_conversations (the fast-path fake) agree on who's in what,
    without maintaining a second real membership table."""
    idx = int(channel_id[1:])
    return [f"U{(idx * 7 + j) % n_users:04d}" for j in range(6)]


def make_fake_web_client(users, channels, ims, mpims, history_cap):
    class FakeWebClient:
        def __init__(self, *a, **k):
            self.retry_handlers = []

        def conversations_list(self, types="", limit=200, cursor=None, **k):
            calls["conversations.list"] += 1
            pool = []
            for t in types.split(","):
                pool += {"public_channel": channels, "private_channel": [],
                         "im": ims, "mpim": mpims}.get(t.strip(), [])
            return _page(pool, "channels", cursor, limit)

        def conversations_members(self, channel=None, **k):
            calls["conversations.members"] += 1
            return Resp({"members": _channel_members(channel, len(users))})

        def users_conversations(self, user=None, types="", cursor=None, limit=200, **k):
            calls["users.conversations"] += 1
            ids: list[str] = []
            for t in types.split(","):
                t = t.strip()
                if t in ("public_channel", "private_channel"):
                    ids += [c["id"] for c in channels if user in _channel_members(c["id"], len(users))]
                elif t == "mpim":
                    ids += [c["id"] for c in mpims if user in _channel_members(c["id"], len(users))]
                elif t == "im":
                    ids += [d["id"] for d in ims if d["user"] == user]
            return _page(ids, "channels", cursor, limit)

        def conversations_info(self, channel=None, **k):
            calls["conversations.info"] += 1
            return Resp({"channel": {"name": "somechan", "is_mpim": False}})

        def conversations_history(self, channel=None, limit=50, **k):
            calls["conversations.history"] += 1
            n = min(limit, history_cap)
            return Resp({"messages": [
                {"ts": f"170000000{i}.0001", "user": f"U{i % 40:04d}", "text": f"msg {i}"}
                for i in range(n)]})

        def conversations_replies(self, channel=None, ts=None, **k):
            calls["conversations.replies"] += 1
            return Resp({"messages": [
                {"ts": f"170000000{i}.0001", "user": f"U{i:04d}", "text": f"reply {i}"}
                for i in range(min(history_cap, 40))]})

        def users_info(self, user=None, **k):
            calls["users.info"] += 1
            return Resp({"user": {"id": user, "name": user, "real_name": user, "profile": {}}})

        def users_list(self, cursor=None, limit=200, **k):
            calls["users.list"] += 1
            return _page(users, "members", cursor, limit)

        def search_messages(self, query=None, count=20, **k):
            calls["search.messages"] += 1
            return Resp({"messages": {"matches": [
                {"ts": f"170000000{i}.0001", "user": f"U{i:04d}", "text": "hit",
                 "channel": {"id": f"C{i:04d}", "name": ""}} for i in range(count)]}})

    return FakeWebClient


def wall_clock(counts: dict[str, int], distributed: bool) -> tuple[float, float, float]:
    """(wall, serial, throttle) seconds. Serial is n * RTT -- what today's
    one-call-at-a-time client costs even with the rate limiter idle.
    Throttle is the floor Slack's own per-method limit imposes once the
    burst allowance is spent; the wall clock is whichever binds."""
    tiers = dict(TIER_PER_MINUTE)
    if distributed:
        tiers["conversations.history"] = 1
        tiers["conversations.replies"] = 1
    serial = sum(counts.values()) * RTT_SECONDS
    throttle = max(
        (((n - 1) / tiers[m] * 60) if n > 1 else 0.0) for m, n in counts.items()
    ) if counts else 0.0
    return max(serial, throttle), serial, throttle


def run_case(label, fn, *, warm, fixtures, distributed):
    users, channels, ims, mpims = fixtures
    calls.clear()
    if warm:
        # A real deployment always configures both cache files (see
        # daemon_main.py) -- pointing them at a path that never gets
        # written to keeps this fully offline while still exercising the
        # "cache file configured" code paths (_resolve_user_name_cached,
        # the users.conversations fast path) rather than the "no cache at
        # all" fallback behavior.
        client = sc.SlackClient(
            "xoxp-fake", user_cache_file="/nonexistent/u.json", channel_cache_file="/nonexistent/c.json"
        )
        client._user_cache = {
            u["id"]: sc.SlackUser(id=u["id"], name=u["name"], real_name=u["real_name"])
            for u in users
        }
        client._channel_name_cache = {c["id"]: c.get("name", "") for c in channels + ims + mpims}
        client._channel_is_mpim_cache = {
            c["id"]: bool(c.get("is_mpim")) for c in channels + ims + mpims
        }
        # Mark both directories as already loaded and fresh -- otherwise
        # the first cache-file-configured lookup would trigger a real
        # (synchronous, since no snapshot has "loaded" yet) refresh walk.
        now = datetime.now(timezone.utc)
        client._user_directory_loaded_from_disk = True
        client._user_directory_fetched_at = now
        client._channel_directory_loaded_from_disk = True
        client._channel_directory_fetched_at = now
    else:
        client = sc.SlackClient("xoxp-fake")  # no cache files: directory caches off
    calls.clear()
    fn(client)
    counts = dict(calls)
    wall, serial, throttle = wall_clock(counts, distributed)
    breakdown = "  ".join(f"{m}×{n}" for m, n in
                          sorted(counts.items(), key=lambda kv: -kv[1]))
    print(f"  {label:<44}{sum(counts.values()):>6} calls  {wall:>7.1f}s"
          f"   ({serial:.1f}s serial / {throttle:.1f}s throttled)")
    if breakdown:
        print(f"  {'':<44}       {breakdown}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=2000)
    ap.add_argument("--channels", type=int, default=900)
    ap.add_argument("--ims", type=int, default=600)
    ap.add_argument("--mpims", type=int, default=40)
    ap.add_argument("--distributed", action="store_true",
                    help="model the Slack app as distributed outside the Marketplace: "
                         "conversations.history/.replies drop to 1 req/min and 15 objects")
    args = ap.parse_args()

    users = [{"id": f"U{i:04d}", "name": f"user{i}", "real_name": f"User {i}",
              "profile": {"email": f"u{i}@example.com"}} for i in range(args.users)]
    channels = [{"id": f"C{i:04d}", "name": f"chan-{i}", "is_private": False}
                for i in range(args.channels)]
    ims = [{"id": f"D{i:04d}", "user": f"U{i:04d}", "is_im": True} for i in range(args.ims)]
    mpims = [{"id": f"G{i:04d}", "name": f"mpdm-{i}", "is_mpim": True} for i in range(args.mpims)]
    history_cap = 15 if args.distributed else 1000
    sc.WebClient = make_fake_web_client(users, channels, ims, mpims, history_cap)
    fixtures = (users, channels, ims, mpims)

    cases = [
        ("slack_search_messages(participant=…)",
         lambda c: c.search_messages(participant="User 7", count=20)),
        ("slack_search_messages(query=…)",
         lambda c: c.search_messages(query="budget", count=20)),
        ("slack_list_channels(participant=…)",
         lambda c: c.list_channels(participant="User 7", max_results=100)),
        ("slack_list_dms()",
         lambda c: c.list_dms(max_results=100)),
        ("slack_list_group_chats()",
         lambda c: c.list_group_chats(max_results=100)),
        ("slack_get_channel_history(limit=50)",
         lambda c: c.get_channel_history("C0001", limit=50)),
        ("slack_get_thread_replies()",
         lambda c: c.get_thread_replies("C0001", "1700000000.0001")),
    ]

    print(f"\nWorkspace: {args.users} users, {args.channels} channels, "
          f"{args.ims} DMs, {args.mpims} group DMs")
    print(f"Slack app: {'distributed outside the Marketplace (history/replies 1/min, 15 objects)' if args.distributed else 'internal or Marketplace-approved (history/replies Tier 3)'}")
    for warm in (True, False):
        state = "directory caches WARM (steady state: user_cache_file/channel_cache_file configured, populated)" if warm else \
                "directory caches DISABLED (user_cache_file/channel_cache_file unset -- a supported but non-default config; "\
                "daemon_main.py always sets both, so production is always in the WARM state above shortly after startup)"
        print(f"\n{state}\n{'-' * 78}")
        for label, fn in cases:
            run_case(label, fn, warm=warm, fixtures=fixtures, distributed=args.distributed)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
