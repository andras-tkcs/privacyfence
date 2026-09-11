# ADR 0001: no macOS-native runtime extra

## Status

Accepted and reflected in the current implementation.

## Decision

PrivacyFence does not declare or require a `macos-native` optional dependency containing `rumps` or PyObjC/AppKit frameworks.

The user-facing approval and settings surfaces are served by the embedded web application. macOS packaging may use normal platform build/signing/notarization tooling, but the Python runtime does not depend on an AppKit-specific UI layer.

## Rationale

Keeping unused native UI dependencies would expand the dependency/audit surface without providing runtime functionality. The browser-based UI is shared across macOS, Windows, and Linux and keeps approval/settings behavior in one implementation.

## Consequences

- `pip install privacyfence[macos-native]` is not a supported installation path.
- Runtime code should not import `rumps` or PyObjC/AppKit frameworks.
- Cross-platform source tests can run without installing macOS-native Python UI packages.
- macOS-specific work remains in the packaging/signing/notarization path rather than a separate Python UI stack.
- A future native UI would require a new architecture decision and dependency review rather than silently reusing an old extra.

## Verification

`pyproject.toml` is authoritative for declared extras/dependencies. The source/test suite should remain free of runtime imports that require the removed native UI stack.
