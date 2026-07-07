# Coding & Testing Guidelines

This document describes the conventions this codebase already follows, so new code (human- or
agent-written) stays consistent with it. It is descriptive before it is prescriptive: every rule
below was extracted from patterns already established in `src/privacyfence/` and `tests/`, not
imported from a generic style guide. Where the codebase itself is inconsistent, that's called out
explicitly rather than papered over.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for process (PRs, issues, license) and
[`docs/security-and-compliance.md`](security-and-compliance.md) for the security model this code
implements. This document is about how to write and test the code correctly, not why it exists.

---

## 1. Coding guidelines

### 1.1 Language baseline

- Python 3.11+. Every module starts with `from __future__ import annotations` (right after the
  module docstring, before other imports).
- Prefer the standard library over new dependencies (stated in `CONTRIBUTING.md`).
- Use modern union syntax, `X | None`, not `Optional[X]`. The codebase has no remaining
  `Optional[...]` call sites — keep it that way in new code.
- Type-hint function signatures, including return types (`-> None`, `-> Any`, etc.). Dataclass
  fields are always typed.

### 1.2 Module & docstring conventions

- Every module has a docstring as the first line of the file — even a one-liner
  (`"""Gmail connector."""`). For modules with non-obvious lifecycle or invariants (e.g.
  `daemon_main.py`, `gate.py`, `audit_log.py`), the docstring explains the *why*: threading model,
  ordering guarantees, what a caller must not assume.
- Default to no comments. Only add one when it captures a non-obvious *why* — a hidden constraint,
  a race that was fixed, a workaround for a specific API quirk — never a restatement of *what* the
  next line does. This is already stated in `CONTRIBUTING.md`; it's repeated here because it's the
  single most-violated rule in most codebases and worth being explicit about.
- Long files use `# --- Section --- #` banner comments to separate phases (e.g. gmail.py's
  "Auto (no gate)" / "Review gate (reads)" / "Popup gate (writes)" / "Helpers"). Use this once a
  connector or client grows past ~5-6 methods; don't bother for short files.

### 1.3 Data modeling

- Use `@dataclass` for structured data crossing a boundary (API responses, tool specs, audit
  entries). Give collection fields `field(default_factory=...)`, never a mutable default.
- Dataclasses that normalize an external API's response document what they deliberately *don't*
  carry (e.g. `Attachment`: "Content is intentionally never carried here") when the omission is a
  privacy decision, not just an oversight.
- `Connector` subclasses are the unit of extension (`connector.py`). Adding a service means: one
  file in `connectors/`, registered in `daemon_main.py`, nothing else changes. Don't special-case
  a connector's wiring elsewhere.

### 1.4 Error handling

- Every external-API client (`*_client.py`) defines its own `<Name>ClientError(Exception)` and
  raises only that (or lets it propagate) across its public methods. Internal-only clients that
  never leave the local trust boundary (e.g. `ipc_client.py`, talking over the local Unix socket
  to the daemon the app itself controls) are the one accepted exception to this — external cloud
  APIs always get a dedicated error type.
- Connectors catch the client's specific error type at the boundary, log it, and re-raise as
  `RuntimeError(str(exc)) from exc` — never swallow it, never let the raw client exception or a
  bare `except Exception` leak past the connector into the tool-call response.
- Non-critical side effects (writing an audit entry) are wrapped in their own
  `try/except Exception: logger.warning(...)` so a logging failure never blocks the primary
  operation — see `gate.py::_audit` and every connector's `_auto_audit`.

### 1.5 The gate is load-bearing — treat it as a security boundary, not plumbing

This is the one area where "looks like a style rule" is actually a security invariant:

- Every tool call that touches real data must go through `gated_call()` (`gate.py`), **or** be a
  connector that deliberately auto-approves a whole tool and says so in its own docstring/comments
  (e.g. `contacts.py`'s unconditionally-auto-accepted tools, or the read-only listing tools every
  connector has). There is no third option — a tool that silently skips both the gate and an
  explicit auto-approve rationale is a bug. Writes in particular should not default to
  auto-approval without a documented reason; see `tasks.py`'s history — it originally
  auto-approved every tool, including writes, until that was found to be an undocumented deviation
  from this project's own stated security posture and brought in line with every other connector's
  write-gating.
- `gated_call()` must never return `raw_data` when `filtered_data` differs from it. This is stated
  verbatim in `tests/unit/test_gate.py`'s module docstring — it's the actual privacy boundary the
  whole gate exists to enforce.
- `preview` dicts (shown before approval) carry metadata only — sender, subject, size, destination
  path. Full body/content only ever goes into `details_text`, which the user must actively expand
  to see. Never put message bodies, file contents, or similar into `preview`.
- Every tool a connector exposes must leave an audit trail, one way or another — either through
  `gated_call` (which always audits, on every branch) or via a direct `_auto_audit`-style call for
  auto-approved tools. `tests/helpers.py::assert_all_tools_leave_an_audit_trail` enforces this
  mechanically; don't add a tool that this helper can't verify.
- Writes default to `gate="popup"`, reads of full content default to `gate="review"`; only
  low-sensitivity metadata listing calls (`list_messages`, `list_task_lists`, ...) default to
  `read_only=True` with no gate at all. If a new tool doesn't fit one of these three buckets
  cleanly, that's a design question worth raising explicitly, not silently defaulting to whichever
  is least effort.

### 1.6 Untrusted input

- Any filename, path fragment, or identifier that originates from a remote party (an email
  attachment name, a message header) is untrusted. Before it touches the filesystem, sanitize it —
  the established pattern is `os.path.basename(filename) or "<fallback>"`
  (`gmail_client.py::resolve_attachment_destination`), which is what stops a crafted name like
  `../../.ssh/authorized_keys` from writing outside the intended directory. Compute the destination
  path once, in one function, and reuse it for both the pre-approval preview and the actual write —
  never compute it twice, or the preview and the real write can silently disagree.

### 1.7 Async & concurrency

- Blocking/sync calls (the Google/Slack/Salesforce/Atlassian SDKs, file I/O in a hot path) are
  wrapped in `asyncio.to_thread(...)`, never called directly from an `async def`.
- There is exactly one popup lock (`gate._popup_lock`) serializing every native dialog. If you add
  a new interactive flow, it must go through the existing lock, not a new one — two independent
  locks around the same native-dialog resource is how the queued-request race class of bug
  reappears.
- Once inside a lock, re-check any state you decided on before acquiring it (see the "re-check
  after acquiring `_popup_lock`" pattern in `gate.py`) if another queued waiter could have changed
  that state while you waited.

### 1.8 Logging vs. `print`

- Library/application code (clients, connectors, the daemon's request handling) logs via
  `logging.getLogger(__name__)`. Never log full message bodies, document contents, or credentials —
  log identifiers, subjects, counts.
- `print()` is reserved for the handful of argparse CLI subcommands in `daemon_main.py`
  (`--oauth-setup`-style flows) that are invoked directly by a human at a terminal and need to
  print a human-facing confirmation. It does not belong inside a `*_client.py` method — those are
  library code, called from multiple contexts, and should only log; the CLI entry point that calls
  them is responsible for any terminal-facing `print`.

---

## 2. Testing guidelines

### 2.1 Framework & layout

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed).
  `pytest-timeout` caps every test at 30s so a hung fixture fails loudly instead of stalling CI.
  `freezegun` for time-dependent tests, `openpyxl` for asserting against the audit log's Excel
  export.
- `tests/unit/` mirrors `src/privacyfence/`; connector tests live in `tests/unit/connectors/`,
  named `test_<connector>_connector.py`. One test module per source module.
- CI (`.github/workflows/`) requires a 100% pass rate on macOS (this app depends on real
  AppKit/PyObjC/osascript behavior) — coverage is reported but not gated. Write tests as if any
  failure blocks the merge, because it does.

### 2.2 Module & class organization

- Every test module opens with a docstring naming the module under test and, where relevant, the
  one invariant that matters most (see `test_gate.py`, `test_gmail_connector.py`). If a whole test
  file exists to prevent one specific class of bug, say so up top.
- Group tests into `class TestScenario:` blocks by behavior (`TestAutoAcceptPath`,
  `TestReviewGateDecisions`, `TestAcceptAll`, ...), not by method-under-test or in one flat list.
  Bare module-level `def test_...` functions are the exception, reserved for
  structural/cross-cutting checks that aren't about one component's behavior (e.g.
  `test_readme_manifest_alignment.py`, which checks docs stay in sync with the tool manifest).
- Regression tests carry a docstring explaining the original bug, not just what they assert — see
  `TestQueuedRequestReCheck` in `test_gate.py` or `TestGetMessagePreviewMinimization`'s "a prior
  real bug: reply-all only checked the original sender" note in `test_gmail_connector.py`. The
  point is that a future reader can tell *why* the test exists before deciding it's safe to delete
  or weaken.

### 2.3 Fixtures & isolation

- Module-level singletons (`auto_accept._INSTANCE`, `audit_log._INSTANCE`, and their supporting
  module globals) are reset in an `autouse=True` fixture in `tests/conftest.py`, before *and*
  after each test. Any new module-level singleton needs a matching reset added there, or state
  leaks between tests silently.
- Use the `tmp_path` fixture with `init_audit_logger(str(tmp_path))` to get an isolated audit log
  directory per test — never point tests at the real `logs/audit/` directory.
- A lock or other primitive that binds itself to whichever asyncio event loop first contends on it
  (e.g. `asyncio.Lock`) needs its own `autouse` per-test reset if tests exercise real contention on
  it — see `_fresh_popup_lock` in `test_gate.py` and the comment explaining why.

### 2.4 Faking the gate and native UI

- Never let a test spawn a real `osascript` dialog. Stub `show_popup` / `show_read_popup` /
  `show_rule_confirmation_popup` via `monkeypatch.setattr` on the module under test, not by mocking
  at the `approval_popup` import site of every caller.
- Connector tests stub `gated_call` itself (`gated_call_spy` pattern in
  `test_gmail_connector.py`) to capture exactly what a tool sends into the gate — `preview`,
  `details_text`, `raw_data`, `filtered_data`, `args`, `gate` — and assert on those kwargs, rather
  than trying to drive the real gate end-to-end from a connector test. `test_gate.py` owns proving
  the gate's own state machine; connector tests own proving each tool calls it correctly.

### 2.5 Reuse shared helpers before writing new ad hoc ones

- `tests/helpers.py` provides `make_ctx` (a `ReviewContext` with sane defaults),
  `build_stub_args` (a minimal-but-plausible args dict from a `ToolSpec`), and
  `assert_all_tools_leave_an_audit_trail`. Check there before writing a new stub-args builder or a
  new per-connector audit-trail sweep — duplicating these tends to drift out of sync with the real
  `Connector`/`ToolSpec` shape over time.

### 2.6 New-connector checklist

A new connector's test module should include, at minimum:
1. `TestDispatch` — unknown tool name raises `ValueError`.
2. One test per auto-approved tool proving it never touches the gate and does write its own audit
   entry.
3. One test per gated tool's `preview` dict, asserting it contains *only* metadata (data
   minimization) — never full body/content.
4. A call to `assert_all_tools_leave_an_audit_trail` covering every tool the connector declares,
   with `arg_overrides` for any tool that validates its args before reaching the client/gate.

### 2.7 Definition of done for a PR touching this repo

- [ ] `pytest -v --cov=src/privacyfence --cov-report=term-missing` passes at 100%.
- [ ] Every new/changed tool call still resolves through `gated_call` or an explicit
      always-auto-approve connector, and leaves an audit trail either way.
- [ ] No preview dict carries full content; no log line carries a credential or a message/document
      body.
- [ ] New client code has a matching `<Name>ClientError`; new connector code catches it and
      re-raises as `RuntimeError`.
- [ ] New module-level singletons have a reset added to `tests/conftest.py`.
- [ ] Comments only where the *why* is non-obvious; no restated-*what* comments.
