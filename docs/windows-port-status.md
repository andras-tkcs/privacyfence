# Windows port status (issue #121)

Tracks what's implemented for the Windows port versus what still needs verification on a real
Windows machine, or is genuinely not started. This document exists because the implementation work
was done from a Linux sandbox with no Windows machine and no macOS machine either — every change was
verified by running the automated test suite (real behavior on the platform it targets, monkeypatched
native-dependency boundaries everywhere else — see `docs/coding-and-testing-guidelines.md` §2.4), not
by clicking through a real running app on either OS. That's a meaningfully different confidence level
than this project's existing macOS support has (built and used daily on real macOS for years); treat
everything below accordingly before calling this release-ready.

## Implemented

- **IPC transport** — already cross-platform before this port started (issue #119: TCP loopback +
  token, replacing a Unix domain socket). No changes needed.
- **`ApprovalUI` seam** — already cross-platform before this port started (issue #119). No new
  subclass needed: `approval_popup.py` itself now dispatches by `sys.platform` between
  `approval_window.py`/`dialog_window.py` (macOS) and `approval_window_windows.py`/
  `dialog_window_windows.py` (Windows, pywebview/WebView2) — `NativeApprovalUI` and `gate.py` are
  unchanged.
- **Approval/dialog windows** — `approval_window_windows.py`/`dialog_window_windows.py` render
  `approval_window_html.py`/`dialog_window_html.py`'s existing HTML/JS completely unmodified, via a
  small JS-bridge polyfill (`webview_bridge_windows.py`) that makes the WKWebView-only
  `window.webkit.messageHandlers.pf.postMessage(...)` call resolve against pywebview's own bridge
  instead. Window sizing is simplified relative to macOS's pixel-exact, non-resizable `NSPanel`
  estimate: a resizable window with a generous flat default height, relying on the shared HTML's own
  internal scroll region for anything taller — see `approval_window_windows.py`'s own module
  docstring for the trade-off.
- **Settings window** — `settings_window_windows.py` renders `settings_window_html.py`'s existing
  markup the same way, wired to the identical `SettingsController` bridge-dispatch contract
  `settings_window.py` uses. The one behavior difference: `install_org_config()`'s file picker is now
  an injectable hook on `SettingsController` (`pick_org_config_file_hook`) rather than an inline
  `osascript` call — unset (the macOS default) behaves exactly as before; `settings_window_windows.py`
  sets it to a `window.create_file_dialog()` call.
- **Tray icon** — `tray_windows.py`, `pystray`-based, same two items (`Settings…`/`Quit PrivacyFence`)
  and `run_menu_bar(...)` entry point as `menu_bar.py`. Runs the tray on its own thread
  (`Icon.run_detached()`) so `webview.start()` can own the main thread — see "Needs real Windows
  verification" below.
- **Instance lock** — `daemon_main.py`'s `_acquire_instance_lock()`/`_release_instance_lock()` branch
  on `sys.platform`: `fcntl.flock` on POSIX (unchanged), `msvcrt.locking()` on Windows. `import fcntl`
  is now conditional so the module is importable on Windows at all.
- **Cross-platform "open a path/URL"** — `platform_open.py` replaces the three
  `subprocess.run(["open", ...])` call sites (`settings_window.py`, `settings_controller.py` x2) with
  a helper dispatching to `os.startfile` (Windows) / `open` (macOS) / `webbrowser.open` (fallback).
- **Cross-platform "call this on the UI thread"** — `settings_controller.py`'s
  `_call_on_main_thread()` replaces two direct `AppHelper.callAfter(...)` calls; on Windows (no
  `AppHelper`) it calls straight through, relying on pywebview's own documented any-thread-safety for
  the window operations that end up running (see "Needs real Windows verification").
- **`paths.py`** — `bundle_macos_dir()`/`app_bundle_path()` now correctly return `None` on Windows
  (they had no callers before this port and still don't; kept for whoever adds one). New
  `bundle_dir()` is the cross-platform "directory the frozen exe lives in" a Windows caller should
  reach for instead.
- **Packaging** — `PrivacyFenceApp.windows.spec` (PyInstaller onedir build, no `BUNDLE` step — no
  `.app`-equivalent concept on Windows). `scripts/build_windows.ps1` is a minimal build-only skeleton.
- **Autostart** — `com.privacyfence.app.task.xml`, a Task Scheduler entry template mirroring the
  `com.privacyfence.app.plist` LaunchAgent's own "static file + hand-copy instructions" posture
  (neither OS gets in-app install/uninstall automation from this project today).
- **CI** — `.github/workflows/tests.yml` now runs the full suite on both `macos-latest` and
  `windows-latest`. `tests/conftest.py`'s `collect_ignore_glob` excludes the four AppKit-only test
  files from collection on non-macOS (a `pytest.mark.skipif` inside those files only skips *running*
  them, not the `ModuleNotFoundError` at *collection* time from their unconditional
  `rumps`/`AppKit`/`WebKit` imports).
- **Dependencies** — `pyproject.toml` now scopes `rumps`/`pyobjc-framework-*` to
  `sys_platform == 'darwin'` and adds `pystray`/`pywebview`/`Pillow` scoped to
  `sys_platform == 'win32'`.
- **Test coverage** — every new pure-Python module has unit tests following this project's existing
  conventions (see `docs/coding-and-testing-guidelines.md` §2.4's updated guarded-import note):
  `test_platform_open.py`, `test_webview_bridge_windows.py`, `test_approval_window_windows.py`,
  `test_dialog_window_windows.py`, `test_settings_window_windows.py`, `test_tray_windows.py`, plus new
  cases in `test_daemon_main.py` (Windows instance-lock branch) and `test_paths.py`
  (`bundle_macos_dir`/`app_bundle_path`/`bundle_dir` on Windows).

## Needs real Windows verification before release

Nothing below is a known bug — each is a design decision made from documentation and API contracts
rather than from running the actual combination on Windows, since no Windows machine was available
during implementation. Confirm each on a real Windows machine before treating this as
release-quality:

1. **pystray (detached thread) + pywebview (main thread) combination.** `tray_windows.py` runs the
   tray via `Icon.run_detached()` and gives `webview.start()` the main thread — the standard
   documented pattern for combining the two libraries, but not something this sandbox could actually
   run end-to-end. Confirm the tray icon appears, its menu responds, and windows created from other
   threads (approval popups, the settings window) actually render.
2. **pywebview's cross-thread safety for window creation/`evaluate_js`.** `approval_window_windows.py`/
   `dialog_window_windows.py`/`settings_controller.py`'s `_call_on_main_thread()` all assume
   pywebview's documented "safe to call from any thread once `start()` is running" behavior holds for
   `webview.create_window()` and `window.evaluate_js()`. If it doesn't hold for the specific pywebview
   version/backend actually shipped, gated calls from the IPC server thread (real production traffic,
   not just tests) would need an explicit main-thread dispatch added.
3. **The WebKit-bridge polyfill (`webview_bridge_windows.py`).** Built from pywebview's documented
   `pywebviewready` event and `window.pywebview.api` shape; never exercised against a real WebView2
   render. If the button row in a real approval dialog doesn't resolve, this is the first place to
   look.
4. **`msvcrt.locking()` single-instance lock.** Standard recipe (the same one `portalocker`'s Windows
   backend uses), covered by unit tests that fake `msvcrt`, but never run against the real Windows
   API. Confirm a second `privacyfence-app` launch actually refuses to start.
5. **WebView2 runtime presence.** pywebview's default Windows backend needs the WebView2 runtime
   installed (present by default on current Windows 10/11, but not guaranteed on an older or
   locked-down machine). Nothing here checks for it or guides a user through installing it if it's
   missing — a missing-runtime error would currently just surface as an unhandled pywebview exception.
6. **Window sizing.** `approval_window_windows.py`'s simplified resizable-with-a-flat-default-height
   approach (vs. macOS's pixel-exact estimate) hasn't been visually checked against any real dialog
   shape — confirm nothing looks obviously cramped or excessively empty for the common cases.

## Explicitly out of scope (not started)

- **Authenticode code-signing.** `build_windows.ps1` produces an unsigned build. Needs a real
  Windows machine and the project owner's own signing certificate — the Windows analog of
  `build_dmg.sh`'s `codesign`/`notarytool` steps.
- **A real installer.** No MSI/Inno Setup/NSIS packaging yet — `build_windows.ps1` stops at the
  PyInstaller onedir output. The DMG-equivalent step.
- **`qa_popup_smoke.py`'s Windows equivalent.** The macOS script drives real clicks via `System
  Events` against real AppKit windows to catch modal-loop wiring bugs `test_approval_window.py`'s
  construction-only coverage can't. No such script exists for the pywebview windows yet (e.g. via
  `pywinauto` or WebView2's own UI Automation support) — `test_approval_window_windows.py`/
  `test_dialog_window_windows.py`'s mocked-webview coverage is the only signal today, and it's
  construction/logic-only, same limitation.
- **ACL-based credential-file permission hardening.** The ~10 `os.chmod(path, 0o600)` calls across the
  codebase (restricting OAuth token/cache files to the owning user) are silent no-ops on Windows
  rather than errors, but provide no real protection there (`os.chmod` only maps the read-only bit on
  Windows). A real fix needs Windows ACL manipulation (e.g. via `win32security`), not attempted here.
- **In-app autostart install/uninstall UI.** Neither OS has this today (see `com.privacyfence.app.plist`'s
  own doc-comment-only install instructions) — `com.privacyfence.app.task.xml` mirrors that same
  posture rather than adding new automation only one platform would get.
