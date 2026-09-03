"""The live connector registry the daemon builds once and shares with every
consumer that needs the current connector set.

This is the one piece of ``ipc_server.IPCServer`` that outlives it (P5,
docs/https-connector-refactor-plan.md §12: "``bridge/``, ``ipc.py``,
``ipc_server.py`` deleted"): everything else that class used to own --
socket framing, the per-launch auth token, connector-call dispatch, retry
dedupe, the meta-tools (``check_policy``/``list_rules``/
``propose_rule_change``), unattended-session bookkeeping -- lived there only
because the bridge protocol needed it, and none of that had a reason to
survive the bridge. ``web/mcp_dispatch.py``'s ``McpDispatcher`` is the one
dispatcher left (it always was, for ``/mcp`` -- see that module's own
docstring), and it now owns unattended-session state too, since a
connection over ``/mcp`` is the only kind that can ever mark itself
unattended once there is no bridge socket to do it from.

What's left, and what this class is, is just the live ``{name: Connector}``
map -- built once by ``daemon_main.build_connectors()`` and swapped
wholesale by ``SettingsController.refresh_connectors()`` whenever a service
is authenticated, re-authenticated, or toggled on/off. Three consumers
share exactly one instance, built by ``daemon_main.run_app()``:

- ``web/mcp_dispatch.py``'s ``McpDispatcher`` polls ``.connectors`` on every
  dispatch (``connectors_provider=lambda: host.connectors``) rather than
  holding its own copy, so the one call to ``set_connectors`` below reaches
  it with nothing else needing a second push -- direct successor of
  ``IPCServer.connectors``'s own docstring, which said the same thing about
  the bridge.
- ``settings_controller.SettingsController`` holds it to push a freshly
  rebuilt connector set live (``refresh_connectors``).
- ``menu_bar.py`` holds a reference only to satisfy
  ``SettingsController``'s fallback constructor (native-only callers/tests)
  -- it never reads it directly, same as it never read ``ipc_server``
  directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connector import Connector


class ConnectorHost:
    """Holds the live ``{name: Connector}`` map. No dispatch, no auth, no
    socket -- see module docstring."""

    def __init__(self, connectors: list["Connector"]) -> None:
        self._connectors: dict[str, "Connector"] = {c.name: c for c in connectors}

    @property
    def connectors(self) -> dict[str, "Connector"]:
        """Read-only view of the live connector set."""
        return self._connectors

    def set_connectors(self, connectors: list["Connector"]) -> None:
        """Swap in a freshly built connector set (e.g. after the settings
        window/page authenticates a service or toggles one on/off). A
        single dict reassignment, so no lock is needed against whatever
        event loop ``McpDispatcher`` is polling ``.connectors`` from."""
        self._connectors = {c.name: c for c in connectors}
