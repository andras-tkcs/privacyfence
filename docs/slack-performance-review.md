# Slack connector performance review

**Scope:** `src/privacyfence/slack_client.py`, `src/privacyfence/connectors/slack.py`,
`src/privacyfence/gate.py`, `src/privacyfence/ipc_server.py`, `bridge/src/*`.
**Symptom under review:** Slack tool calls are unusably slow; the approval popup for a search
appears long after the calling MCP client has already given up on the tool call, and the same
search can pop two approval windows.

Numbers below are reproducible offline with `scripts/slack_call_budget.py`.

---

## 1. Executive summary

The approval popup is late because **everything the popup describes is fetched before the popup is
shown, and that fetch is a serial fan-out of dozens to hundreds of individual Slack Web API calls,
most of which exist only to turn ids into display names.**

On a mid-size workspace (2000 members, 900 channels, 600 DMs, 40 group DMs), one
`slack_search_messages(participant=…)` call issues **55 Slack API calls with warm caches and 644
with cold caches** — modelled at **23 s** and **6 minutes** respectively before `gated_call` even
reaches `show_read_popup`.

The MCP SDK the bridge is built on defaults to a **60 s request timeout**, and Claude Desktop's is
not configurable. The bridge sends no progress notifications, so nothing extends that clock; and it
ignores `extra.signal`, so when the client gives up the daemon keeps going and shows the approval
window anyway. Then, because `ipc_server` disables completed-result reuse for every `read_only`
tool, the client's retry re-runs the whole fetch and shows a **second** popup.

That chain — slow fetch → client timeout → orphaned popup → retry → second fetch → second popup —
is exactly the reported behaviour. Every link in it is fixable, and the first three fixes in §5 are
enough to bring the common cases under a second.

---

## 2. Measured API-call budget

`scripts/slack_call_budget.py`, default workspace shape, `RTT = 250 ms`, Slack's published tier
limits. "Wall" is `max(serial round trips, per-method rate-limit floor)`. All of it happens **before**
the approval popup.

### Directory caches warm (steady state)

| Tool call | Slack API calls | Wall | Dominant fan-out |
|---|---:|---:|---|
| `slack_search_messages(participant=…)` | **55** | **23.4 s** | `conversations.members`×40, `conversations.history`×11 |
| `slack_list_channels(participant=…)` | **101** | **59.4 s** | `conversations.members`×100 |
| `slack_list_group_chats()` | **41** | **23.4 s** | `conversations.members`×40 |
| `slack_search_messages(query=…)` | 1 | 0.2 s | — |
| `slack_get_channel_history(limit=50)` | 1 | 0.2 s | — |

### Directory caches cold (first use, or the weekly TTL just expired)

| Tool call | Slack API calls | Wall | Dominant fan-out |
|---|---:|---:|---|
| `slack_search_messages(participant=…)` | **644** | **359 s** | `users.info`×600 |
| `slack_list_channels(participant=…)` | 107 | 59.4 s | `conversations.members`×100 |
| `slack_list_dms()` | **101** | **59.4 s** | `users.info`×100 |
| `slack_get_channel_history(limit=50)` | 42 | 23.4 s | `users.info`×40 |
| `slack_get_thread_replies()` | 42 | 23.4 s | `users.info`×40 |
| `slack_search_messages(query=…)` | 41 | 22.8 s | `conversations.info`×20, `users.info`×20 |

Note the cold `slack_list_dms()` row: even a tool documented as "auto-approved, no popup" costs a
minute, because `_parse_dm` resolves a display name for every DM in the workspace.

---

## 3. Root causes

### R1 — Per-item API fan-out on the approval path

Three patterns produce O(n) Slack calls per tool call:

- `SlackClient._parse_group_chat` (`slack_client.py:1178`) calls `conversations.members` **per group
  chat**, then `_resolve_user_name` **per member**.
- `SlackClient._channel_matches_participant` (`slack_client.py:1154`) calls `conversations.members`
  **per channel**, then `_resolve_user_name` per member when an id-only match doesn't decide it.
- `SlackClient._parse_message` (`slack_client.py:1208`) and `_parse_dm` call `_resolve_user_name`
  **per message / per DM**.

The directory caches were built to absorb exactly this, and they do help — but they only convert a
miss into a hit. The fan-out itself is still there, it is still serial, and `conversations.members`
is never cached at all, which is why the warm column above is still tens of seconds.

`_search_by_participant` (`slack_client.py:626`) compounds it: it hard-codes `max_results=1000` for
both `list_dms` and `list_group_chats`, then issues one `conversations.history` per matched
conversation. Because `_matches_participant` matches display names by **substring**, a needle like
`"user 7"` matched 11 DMs in the model run — 11 history calls for what the user meant as one person.

### R2 — No connection pooling

`slack_sdk`'s synchronous `WebClient` performs each request through
`urllib.request.build_opener(...)` created per call (verified against `slack_sdk` 3.44.0,
`slack_sdk/web/base_client.py`). There is no session and no keep-alive, so **every one of those 55 /
101 / 644 calls pays a fresh TCP + TLS handshake**. At a conservative 100–150 ms of handshake per
call, that alone is 6–15 s on the warm search path and over a minute on the cold one.

### R3 — Nothing runs concurrently

`SlackConnector._fetch` (`connectors/slack.py:666`) wraps each top-level client method in a single
`asyncio.to_thread`. Inside that thread every nested Slack call is strictly sequential. There is no
`asyncio.gather`, no `ThreadPoolExecutor.map`, no batching anywhere in the connector. The 40
`conversations.members` calls in a group-chat listing are 40 blocking round trips in a row.

### R4 — Slack's rate limits turn fan-out into minutes

Slack assigns each method a tier: **T1 1/min, T2 20/min, T3 50/min, T4 100/min**.
`conversations.members` is T4, so `slack_list_channels(participant=…)` with 100 channels sits
*exactly* on the ceiling and starts drawing 429s.

When a 429 arrives, the configured `RateLimitErrorRetryHandler(max_retry_count=3)`
(`slack_client.py:314`) calls **`time.sleep(Retry-After)` on the worker thread** (verified in
`slack_sdk/http_retry/builtin_handlers.py`). Slack's `Retry-After` for these is typically 30–60 s,
so a single unlucky call can silently block for ~3 minutes with no log line and no way for the user
to tell the difference between "working" and "hung".

**Deployment risk worth checking first:** since 2025-05-29, `conversations.history` and
`conversations.replies` are limited to **1 request per minute and a hard 15-object cap** for apps
*distributed outside the Slack Marketplace*, enforced on existing installs from 2025-09-02.
Internal, customer-built apps that live in a single workspace keep Tier 3 and are explicitly exempt
(2025-06-03 clarification). `docs/slack-setup.md` tells admins to create the app "From scratch" in
their own workspace, which keeps it internal — **but if anyone ever clicked "Activate Public
Distribution"** (or the org bundle is shared across workspaces), the app is distributed and no
client-side optimisation will save it. Running `scripts/slack_call_budget.py --distributed` models
that world: the same warm participant search goes from 23 s to **600 s**, and
`slack_get_channel_history(limit=50)` silently returns 15 messages.

### R5 — A stale directory refresh runs *inline*, inside a gated call

`_ensure_user_directory` / `_ensure_channel_directory` (`slack_client.py:945`, `:966`) are called
lazily from `get_user_info`, `resolve_channel_name` and `resolve_is_group_dm`. Once the 7-day TTL
expires, the **next tool call to touch any of them pays a full multi-page `users.list` +
`conversations.list` walk** — modelled at ~27 s for this workspace — before the popup appears.
`_warm_connector_caches` only covers daemon startup; a session that runs across the TTL boundary
takes the hit on the request path.

### R6 — Slow Slack calls can starve the approval popup itself

`gate.py` dispatches the popup with `await asyncio.to_thread(show_read_popup, …)`
(`gate.py:565`), and `_confirm_pii_or_deny` / `show_rule_confirmation_popup` do the same.
`asyncio.to_thread` uses the loop's **default** `ThreadPoolExecutor` — `min(32, cpu_count + 4)`, so
**12 workers on an 8-core Mac** — which is the *same* pool every connector's `_fetch` uses.

A handful of concurrent Slack calls sitting in `time.sleep(60)` rate-limit waits can occupy every
worker. When that happens the popup thread cannot start at all: the approval window is delayed not
by Slack, but by PrivacyFence queueing behind itself. Gmail, Drive and every other connector stall
for the same reason.

### R7 — The bridge neither heartbeats nor cancels

`registerConnectorTool` (`bridge/src/tools.ts:105`) is `async (args) => { … }` — it never takes the
handler's second `extra` argument. Three capabilities go unused as a result, all present in the
already-pinned `@modelcontextprotocol/sdk` 1.29.0:

| Capability | Consequence today |
|---|---|
| `extra.sendNotification({method:"notifications/progress", …})` | Nothing resets the client's request timer. The SDK default is 60 s; Claude Desktop's is not configurable. |
| `extra.signal` (AbortSignal) | When the client gives up, the daemon has no idea and still opens an approval window for a result nobody will read. |
| `extra._meta.progressToken` | — |

I verified both mechanics against the pinned SDK: a tool that emits progress survives a client
whose timeout is 300 ms, an otherwise-identical silent tool does not, and `extra.signal` does fire
on client cancellation. **Caveat:** progress only helps on hosts that send a `progressToken` and set
`resetTimeoutOnProgress`. Claude Code does; Cowork reportedly does not
([anthropics/claude-code#58687](https://github.com/anthropics/claude-code/issues/58687), closed as
not planned). So a heartbeat is a free, correct defensive measure — but **it cannot be the primary
fix**. The calls have to get fast.

There is also **no cancel verb in the IPC protocol at all** (`ipc.py`), so even a heartbeat-aware
bridge currently has nothing to forward an abort to.

### R8 — Timeout retries re-run the fetch and pop a second popup

`IPCServer._call_connector` (`ipc_server.py:229`) dedupes identical calls, but disables
*completed-result* reuse for every `read_only` tool. The stated reason is staleness: a read repeated
after an unrelated write must see the write. The effect on this path is worse than the staleness it
prevents:

- Client times out **during** the fetch or popup → the retry coalesces onto the in-flight future. Fine.
- Client times out **just after** the user approved → the retry finds a completed future it is not
  allowed to reuse, re-runs the entire 23-second fetch, and **shows the approval popup a second
  time** for a decision the user already made.

That is the "two approval popups for one search" in the report. The guard should key on *"has a
write to this connector happened since this result was produced"*, not on `read_only`.

### R9 — Blocking work on the daemon's event loop

`gated_call` runs on the IPC server's single event loop and calls `detect_pii_categories`
(measured 78 ms for 1000 messages), optionally `scan_pii_for_audit`, and
`get_audit_logger().recent_matches()` — a full linear scan and JSON parse of the week's audit log
(measured **225 ms at 50 000 rows**) — all synchronously. Every concurrent IPC request stalls for
that long. Small next to R1–R4, but it is pure overhead on the critical path.

---

## 4. Correctness bugs found alongside

Independent of performance; both of the first two are demonstrated by targeted runs against a fake
`WebClient`.

1. **`list_channels` truncates before it filters.** `slack_client.py:405` does
   `channels = channels[:max_results]` and *then* applies the participant filter, so a participant
   who is only in channel #250 is invisible at `max_results=100` — confirmed: returns `[]` at 100,
   `['C0250']` at 300. `list_dms` filters *then* truncates. The two tools have silently different
   semantics for the same parameter.

2. **`conversations.members` is never paginated.** `_resolve_members` (`slack_client.py:1130`) calls
   `conversations_members(channel=…, limit=1000)` and ignores `response_metadata.next_cursor`. Any
   member past the first 1000 is invisible, so participant filtering on a large channel returns a
   wrong *negative* — confirmed against a 1200-member fixture.

3. **`slack_send_message(thread_ts=…)` fetches the entire thread for a cosmetic preview line.**
   `connectors/slack.py:629` calls `get_thread_replies` purely to read `messages[0].text` for the
   popup's "In thread" row. That is a full `conversations.replies` call — the single most
   rate-limited method under the 2025 rules — plus a `users.info` per reply on a cache miss, on the
   *write* path, before the send popup can open.

4. **Slack's 15-message cap is invisible to Claude.** If the app is ever treated as distributed,
   `slack_get_channel_history(limit=50)` returns 15 messages with nothing in the result saying it
   was truncated, so Claude reasons as though it read the whole window.

5. **Substring participant matching fans out.** `_matches_participant` matches display names by
   substring, so `"user 7"` matches `User 7`, `User 70`…`User 79`. Each extra match costs a
   `conversations.history` call in `_search_by_participant`. Exact/prefix match with a substring
   fallback only when nothing matched exactly would fix both the cost and the false positives.

6. **The directory caches are not thread-safe.** `_user_cache`, `_channel_name_cache`,
   `_channel_is_mpim_cache` and `_channel_refresh_cursor` are mutated from the `slack-cache-warm`
   thread (`daemon_main.py:497`) and from every `asyncio.to_thread` worker with no lock. The
   check-then-act in `_ensure_*_directory` can also start two full walks concurrently — the
   `_DIRECTORY_RETRY_COOLDOWN` only closes that window after the first attempt has been recorded.
   `refresh_channel_directory`'s read-modify-write of `_channel_refresh_cursor` can lose pages when
   the warm thread and the `slack_refresh_channel_cache` tool overlap.

7. **Cold-cache participant search: maximum cost, zero value.** In the cold run, the 644-call
   participant search returned **no results** — with the user directory empty, `_resolve_user_name`
   falls back to a raw id, so a name needle matches nothing. It spends six minutes to answer
   "nothing found", which is also wrong.

---

## 5. Recommended fixes, in priority order

### P0 — makes the connector usable

1. **Resolve participants through an index, not through the API.** Build a reverse index
   (`display name` / `handle` / `email` → `user_id`) once from the cached user directory, resolve
   the needle to ids up front, then match by id. This deletes the `users.info`×600 fan-out and lets
   `list_group_chats`/`list_channels` skip most `conversations.members` calls entirely.
   *Removes the single largest term in both the warm and cold columns.*

2. **Never make a live per-item lookup on the approval path.** `_resolve_user_name`,
   `resolve_channel_name` and `resolve_is_group_dm` should be **cache-only** while a gated fetch is
   in progress, degrading to the raw id and letting the result carry a "run `slack_refresh_user_cache`"
   hint. Name decoration is not worth a minute of the user's time, and it is never load-bearing for
   the approval decision — the popup already shows the id when a name is unavailable.

3. **Cap the fan-out explicitly.** `_search_by_participant` should stop hard-coding
   `max_results=1000` and should bound the number of conversations it reads history from (say 5),
   reporting the cap in the result. Tie `count` to the number of conversations rather than
   multiplying by it.

4. **Pool connections and parallelise.** Either hand `WebClient` a pooled transport, or move the
   connector to `slack_sdk.web.async_client.AsyncWebClient` (aiohttp, pooled, natively concurrent)
   and `asyncio.gather` the per-conversation fetches behind a small semaphore (4–6). Combined with
   (1)–(3) this is the difference between 23 s and well under a second.

5. **Get the stale-directory refresh off the request path.** `_ensure_*_directory` should never
   refresh inline: serve the stale snapshot, schedule the refresh on the existing background warm
   thread, and make it single-flight under a lock.

6. **Give the popups their own executor.** A dedicated single-thread `ThreadPoolExecutor` for
   `show_read_popup` / `show_popup` / the confirmation dialogs, passed via
   `loop.run_in_executor`, so connector I/O can never starve the UI (R6). Cheap, and it fixes a
   whole class of "the app feels frozen" reports across every connector.

### P1 — makes the failure modes survivable

7. **Emit `notifications/progress` from the bridge** every ~5 s while a call is outstanding. Free on
   hosts that ignore it, decisive on hosts that honour it.
8. **Wire `extra.signal` to a new `cancel` IPC method** so an abandoned call stops instead of
   opening a stale approval window.
9. **Replace the blanket `read_only` dedupe exemption** with write-invalidation per connector, so a
   timeout retry reuses the already-approved result instead of re-popping (R8).
10. **Surface truncation** when Slack returns fewer messages than `limit` requested.
11. **Lock the directory caches; make `_ensure_*` single-flight.**

### P2 — correctness and hygiene

12. Fix `list_channels`' filter-then-truncate ordering; paginate `conversations.members` (§4.1, §4.2).
13. Drop the thread fetch from `slack_send_message`'s preview, or make it opt-in (§4.3).
14. Move `detect_pii_categories` and `recent_matches` off the event loop (R9).
15. **Verify the org's Slack app is internal** — single workspace, public distribution never
    activated — and say so explicitly in `docs/slack-setup.md`, with a warning that activating
    distribution drops `conversations.history`/`.replies` to 1 req/min and 15 messages.

---

## Sources

- [Rate limits — Slack Developer Docs](https://docs.slack.dev/apis/web-api/rate-limits/)
- [Rate limit changes for non-Marketplace apps (2025-05-29)](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/)
- [Clarifying rate limit changes for non-Marketplace apps (2025-06-03)](https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/)
- [conversations.history returns only 15 messages — community discussion](https://github.com/orgs/community/discussions/162325)
- [slack_sdk http_retry builtin handlers](https://docs.slack.dev/tools/python-slack-sdk/reference/http_retry/builtin_handlers.html)
- [Cowork MCP client times out long-running tool calls despite progress notifications (#58687)](https://github.com/anthropics/claude-code/issues/58687)
- [Make MCP tool call timeout configurable (MCP_TOOL_TIMEOUT) (#47076)](https://github.com/anthropics/claude-code/issues/47076)
- [timeout field in claude_desktop_config.json not honored (#43791)](https://github.com/anthropics/claude-code/issues/43791)
- [Slack users.list rate-limit loop on large workspaces (openclaw#31733)](https://github.com/openclaw/openclaw/issues/31733)
- [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp)
