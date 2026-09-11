# ADR 0001: Remove the `macos-native` optional dependency extra

## Status

Accepted (Phase 3, PR3.9 of the now-removed `docs/security-remediation-plan.md`, ORP-02).

## Context

P10 of the (now-removed) `https-connector-refactor-plan.md` deleted the native macOS menu bar /
approval dialogs / settings window (`menu_bar.py`, `approval_window.py`, `dialog_window.py`,
`approval_popup.py` and friends — decision D6: "two approval surfaces means two places for a
security fix to land"). Nothing under `src/privacyfence/` has imported `rumps` or any
`pyobjc-framework-*` package since. `pyproject.toml` nonetheless kept a `macos-native` optional
extra bundling those packages, on the reasoning that D6's own justification for deleting the native
UI ("the `ApprovalUI` seam lets it come back if that proves wrong") meant a future native
implementation — e.g. a Windows-native dialog for #121, or a macOS one — might want the same
dependency set back, so it was cheaper to leave the extra declared than to reconstruct it from
scratch later.

The 2026-09-04 technical review flagged this as orphaned/dormant code (§13, ORP-02): an optional
extra nobody installs, pinning packages nothing imports, is a maintenance and audit-surface cost
(dependency-scanning tools still resolve and report on it, contributors have to know to skip it)
for a hypothetical that has not materialized in the time since P10. The review's recommendation was
to drop it, with this ADR serving as the record of exactly what was removed, so a future native-UI
effort has a documented starting point rather than having to reconstruct the dependency set from
old git history.

## Decision

Delete the `macos-native` optional-dependencies group from `pyproject.toml`. Record the exact
package set it declared here for future reference:

```toml
macos-native = [
    "rumps>=0.4.0; sys_platform == 'darwin'",
    "pyobjc-framework-Cocoa>=10.0; sys_platform == 'darwin'",
    "pyobjc-framework-WebKit>=10.0; sys_platform == 'darwin'",
    "pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'",
]
```

- `rumps>=0.4.0` — the macOS menu bar app framework `menu_bar.py` was built on.
- `pyobjc-framework-Cocoa>=10.0` — AppKit bindings used by `approval_window.py`/`dialog_window.py`
  for the native window chrome.
- `pyobjc-framework-WebKit>=10.0` — the embedded `WKWebView` those native windows hosted approval
  content in.
- `pyobjc-framework-Quartz>=10.0` — screen/display APIs used for window positioning.

All four were already gated to `sys_platform == 'darwin'`, so this was never a dependency on
non-macOS platforms even while declared.

## Consequences

- `pip install privacyfence[macos-native]` becomes an error (unknown extra) instead of a no-op
  install of unused packages. Nothing in the shipped `.app`/`.dmg` build or any CI workflow
  referenced this extra, so this has no runtime or release-pipeline impact.
- A future native-UI implementation (Windows-native per #121, or a reintroduced macOS one) should
  re-add whatever subset of the package list above it actually needs as a fresh extra, re-checking
  version floors against whatever PyObjC/rumps releases exist at that time rather than assuming
  these are still current — this ADR documents where those packages came from, not a promise that
  these exact versions remain the right ones to reinstate.
