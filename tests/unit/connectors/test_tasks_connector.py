"""Unit tests for privacyfence.connectors.tasks.TasksConnector.

Reads (list task lists, list tasks, get task) are unconditionally
auto-approved and funnel through the single _run() helper, which serializes
dataclass results with dataclasses.asdict() and records an audit entry
directly — no gate.gated_call involvement.

Writes (create/update/complete/uncomplete/move) go through gate.gated_call
with gate="popup", same as every other connector's writes. gated_call itself
is stubbed here (never spawn a real osascript dialog from a unit test); these
tests instead assert that each write tool sends a minimal, non-body-carrying
preview into the gate, and that a denial genuinely blocks the underlying
client call from ever happening.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import tasks as tasks_module
from privacyfence.connectors.tasks import TasksConnector
from privacyfence.tasks_client import Task, TaskList, TasksClient, TasksClientError

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_no_placeholder_fields

LIVE_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "live" / "tasks"


def make_connector():
    client = MagicMock()
    # Default to "not resolvable" so tests that don't care about task-list-name
    # resolution keep seeing the raw list id, same as before this was added.
    client.get_task_list.side_effect = TasksClientError("no such task list")
    return TasksConnector(client), client


def make_task(**overrides):
    defaults = dict(
        id="t1", task_list_id="list1", title="Buy milk", notes="2%",
        due="2026-07-10T00:00:00Z", status="needsAction", completed="",
        updated="2026-07-06T00:00:00Z", position="0", parent="",
    )
    defaults.update(overrides)
    return Task(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    """Stub gated_call to record its kwargs and act as if the user approved."""
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(tasks_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Tasks tool"):
            await connector.call("tasks_does_not_exist", {})


class TestListAndGet:
    async def test_list_task_lists_serializes_dataclasses(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_task_lists.return_value = [TaskList(id="l1", title="Groceries", updated="u1")]

        result = await connector.call("tasks_list_task_lists", {})

        assert result == [{"id": "l1", "title": "Groceries", "updated": "u1"}]
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"sender": ""' in entries[0]

    async def test_list_tasks_passes_show_completed_and_serializes_list(self):
        connector, client = make_connector()
        client.list_tasks.return_value = [make_task()]

        result = await connector.call("tasks_list_tasks", {"task_list_id": "list1", "show_completed": True})

        client.list_tasks.assert_called_once_with("list1", True)
        assert result == [make_task().__dict__]

    async def test_get_task_serializes_single_dataclass(self):
        connector, client = make_connector()
        client.get_task.return_value = make_task()

        result = await connector.call("tasks_get_task", {"task_list_id": "list1", "task_id": "t1"})

        client.get_task.assert_called_once_with("list1", "t1")
        assert result == make_task().__dict__


class TestListNameResolution:
    async def test_task_list_name_resolved_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task_list.side_effect = None
        client.get_task_list.return_value = TaskList(id="list1", title="Groceries", updated="u")
        client.create_task.return_value = make_task()

        await connector.call("tasks_create_task", {"task_list_id": "list1", "title": "Buy milk"})

        assert gated_call_spy[0]["preview"]["Task list"] == "Groceries"

    async def test_unresolvable_list_falls_back_to_raw_id(self, gated_call_spy):
        connector, client = make_connector()  # default: get_task_list always fails
        client.create_task.return_value = make_task()

        await connector.call("tasks_create_task", {"task_list_id": "list1", "title": "Buy milk"})

        assert gated_call_spy[0]["preview"]["Task list"] == "list1"

    async def test_resolution_is_cached_across_calls(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task_list.side_effect = None
        client.get_task_list.return_value = TaskList(id="list1", title="Groceries", updated="u")
        client.get_task.return_value = make_task()
        client.complete_task.return_value = make_task(status="completed")
        client.uncomplete_task.return_value = make_task(status="needsAction")

        await connector.call("tasks_complete_task", {"task_list_id": "list1", "task_id": "t1"})
        await connector.call("tasks_uncomplete_task", {"task_list_id": "list1", "task_id": "t1"})

        assert gated_call_spy[0]["preview"]["Task list"] == "Groceries"
        assert gated_call_spy[1]["preview"]["Task list"] == "Groceries"
        client.get_task_list.assert_called_once_with("list1")

    async def test_move_task_resolves_both_source_and_destination_lists(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task_list.side_effect = lambda list_id: {
            "list1": TaskList(id="list1", title="Groceries", updated="u"),
            "list2": TaskList(id="list2", title="Errands", updated="u"),
        }[list_id]
        client.get_task.return_value = make_task()
        client.move_task.return_value = make_task(task_list_id="list2")

        await connector.call(
            "tasks_move_task", {"source_list_id": "list1", "task_id": "t1", "destination_list_id": "list2"}
        )

        assert gated_call_spy[0]["preview"] == {
            "Task": "Buy milk", "From list": "Groceries", "To list": "Errands",
        }


class TestCreateAndUpdate:
    async def test_create_task_gates_before_writing_with_metadata_only_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.create_task.return_value = make_task()

        await connector.call("tasks_create_task", {
            "task_list_id": "list1", "title": "Buy milk",
            "notes": "Secret grocery list details", "due": "2026-07-10T00:00:00Z",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"] == {
            "Task list": "list1", "Title": "Buy milk", "Due": "2026-07-10T00:00:00Z",
        }
        # Notes go into details_text (shown only after "Show Details"), never the preview.
        assert "Secret grocery list details" not in kwargs["preview"].values()
        assert kwargs["details_text"] == "Secret grocery list details"
        client.create_task.assert_called_once_with(
            "list1", "Buy milk", "Secret grocery list details", "2026-07-10T00:00:00Z"
        )

    async def test_update_task_coerces_empty_strings_to_none(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task.return_value = make_task()
        client.update_task.return_value = make_task()

        await connector.call("tasks_update_task", {"task_list_id": "list1", "task_id": "t1"})

        client.update_task.assert_called_once_with("list1", "t1", None, None, None)
        assert gated_call_spy[0]["gate"] == "popup"

    async def test_update_task_passes_through_provided_values(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task.return_value = make_task()
        client.update_task.return_value = make_task()

        await connector.call("tasks_update_task", {
            "task_list_id": "list1", "task_id": "t1", "title": "New title", "notes": "n", "due": "d",
        })

        client.update_task.assert_called_once_with("list1", "t1", "New title", "n", "d")


class TestCompleteUncompleteMove:
    async def test_complete_task(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task.return_value = make_task()
        client.complete_task.return_value = make_task(status="completed")

        result = await connector.call("tasks_complete_task", {"task_list_id": "list1", "task_id": "t1"})

        client.complete_task.assert_called_once_with("list1", "t1")
        assert result["status"] == "completed"
        assert gated_call_spy[0]["gate"] == "popup"
        assert gated_call_spy[0]["details_text"] == (
            "Task will be marked as completed; title and notes are unchanged."
        )

    async def test_uncomplete_task(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task.return_value = make_task()
        client.uncomplete_task.return_value = make_task(status="needsAction")

        await connector.call("tasks_uncomplete_task", {"task_list_id": "list1", "task_id": "t1"})

        client.uncomplete_task.assert_called_once_with("list1", "t1")
        assert gated_call_spy[0]["gate"] == "popup"
        assert gated_call_spy[0]["details_text"] == (
            "Task will be marked as not completed; title and notes are unchanged."
        )

    async def test_move_task(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task.return_value = make_task()
        client.move_task.return_value = make_task(task_list_id="list2")

        result = await connector.call(
            "tasks_move_task", {"source_list_id": "list1", "task_id": "t1", "destination_list_id": "list2"}
        )

        client.move_task.assert_called_once_with("list1", "t1", "list2")
        assert result["task_list_id"] == "list2"
        assert gated_call_spy[0]["preview"] == {
            "Task": "Buy milk", "From list": "list1", "To list": "list2",
        }
        assert gated_call_spy[0]["details_text"] == (
            "Task will be moved to the new list; title and notes are unchanged."
        )


class TestPopupGateBlocksWrites:
    """The point of gating these tools at all: a denial must stop the write
    from ever reaching the client, not just get logged after the fact."""

    async def test_denied_write_raises_and_client_is_never_called(self, monkeypatch):
        connector, client = make_connector()
        client.get_task.return_value = make_task()

        async def deny(**kwargs):
            raise RuntimeError("Request denied by user")

        monkeypatch.setattr(tasks_module, "gated_call", deny)

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("tasks_complete_task", {"task_list_id": "list1", "task_id": "t1"})

        client.complete_task.assert_not_called()


class TestNonDataclassResultPassesThroughUnchanged:
    async def test_plain_dict_result_is_not_mangled(self, gated_call_spy):
        connector, client = make_connector()
        client.get_task.return_value = make_task()
        client.complete_task.return_value = {"ok": True}

        result = await connector.call("tasks_complete_task", {"task_list_id": "list1", "task_id": "t1"})

        assert result == {"ok": True}


class TestFieldCompleteness:
    """End to end: a fully-populated raw Tasks API task -> the real
    TasksClient._parse_task -> the real connector's returned data -- not a
    hand-built Task, unlike every other test in this file. Mirrors
    test_confluence_connector.py's TestFieldCompleteness -- the shape of
    check that would catch a _parse_task field mapping silently degrading
    to a fallback before it ships, not after.

    Unlike a connector with a gated read tool, tasks_get_task is
    unconditionally auto-approved (see this module's docstring) and funnels
    through _run()/_serialize() rather than gate.gated_call -- there's no
    gate preview to inspect here, so the returned (serialized) dict is this
    connector's closest analog to a preview for this purpose.
    """

    async def test_get_task_result_has_no_placeholder_fields(self):
        path = LIVE_FIXTURES_DIR / "get_task.json"
        if not path.exists():
            pytest.skip(f"{path} not recorded yet -- run `python3 scripts/qa_fixture_recorder.py --record tasks` locally first")
        raw = json.loads(path.read_text(encoding="utf-8"))
        # The recorded fixture is an incomplete top-level task (no due date,
        # no completed timestamp, no parent) -- those are legitimately blank
        # in that state, but every Task field needs something real here to
        # actually exercise the mapping.
        raw = dict(
            raw,
            due="2027-01-01T00:00:00.000Z",
            status="completed",
            completed="2026-12-31T00:00:00.000Z",
            parent="qa-placeholder-parent-id",
        )

        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = raw
        client = TasksClient(client_config={}, token_file="/tmp/unused-token.json")
        # get_task() runs inside a worker thread (connector._fetch uses
        # asyncio.to_thread), so client._local.service -- thread-local --
        # wouldn't be visible there; overriding _get_service directly is the
        # thread-agnostic equivalent of test_tasks_client.py's make_client().
        client._get_service = lambda: service

        connector = TasksConnector(client)
        result = await connector.call(
            "tasks_get_task", {"task_list_id": "list1", "task_id": raw["id"]}
        )

        assert_no_placeholder_fields(result)


class TestErrorMapping:
    async def test_tasks_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_task_lists.side_effect = TasksClientError("token expired")

        with pytest.raises(RuntimeError, match="token expired"):
            await connector.call("tasks_list_task_lists", {})


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.get_task.return_value = make_task()
        await assert_all_tools_leave_an_audit_trail(connector, tasks_module, monkeypatch, tmp_path)
