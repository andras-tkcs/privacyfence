"""Unit tests for privacyfence.connectors.tasks.TasksConnector.

Every Tasks tool is unconditionally auto-approved (README: "Always
allowed") and funnels through the single _run() helper, which serializes
dataclass results with dataclasses.asdict() and records an audit entry.
No gate.gated_call involvement at all for this connector.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import tasks as tasks_module
from privacyfence.connectors.tasks import TasksConnector
from privacyfence.tasks_client import Task, TaskList, TasksClientError

from ...helpers import assert_all_tools_leave_an_audit_trail


def make_connector():
    client = MagicMock()
    return TasksConnector(client), client


def make_task(**overrides):
    defaults = dict(
        id="t1", task_list_id="list1", title="Buy milk", notes="2%",
        due="2026-07-10T00:00:00Z", status="needsAction", completed="",
        updated="2026-07-06T00:00:00Z", position="0", parent="",
    )
    defaults.update(overrides)
    return Task(**defaults)


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


class TestCreateAndUpdate:
    async def test_create_task_passes_all_fields(self):
        connector, client = make_connector()
        client.create_task.return_value = make_task()

        await connector.call("tasks_create_task", {
            "task_list_id": "list1", "title": "Buy milk", "notes": "2%", "due": "2026-07-10T00:00:00Z",
        })

        client.create_task.assert_called_once_with("list1", "Buy milk", "2%", "2026-07-10T00:00:00Z")

    async def test_update_task_coerces_empty_strings_to_none(self):
        connector, client = make_connector()
        client.update_task.return_value = make_task()

        await connector.call("tasks_update_task", {"task_list_id": "list1", "task_id": "t1"})

        client.update_task.assert_called_once_with("list1", "t1", None, None, None)

    async def test_update_task_passes_through_provided_values(self):
        connector, client = make_connector()
        client.update_task.return_value = make_task()

        await connector.call("tasks_update_task", {
            "task_list_id": "list1", "task_id": "t1", "title": "New title", "notes": "n", "due": "d",
        })

        client.update_task.assert_called_once_with("list1", "t1", "New title", "n", "d")


class TestCompleteUncompleteMove:
    async def test_complete_task(self):
        connector, client = make_connector()
        client.complete_task.return_value = make_task(status="completed")

        result = await connector.call("tasks_complete_task", {"task_list_id": "list1", "task_id": "t1"})

        client.complete_task.assert_called_once_with("list1", "t1")
        assert result["status"] == "completed"

    async def test_uncomplete_task(self):
        connector, client = make_connector()
        client.uncomplete_task.return_value = make_task(status="needsAction")

        await connector.call("tasks_uncomplete_task", {"task_list_id": "list1", "task_id": "t1"})

        client.uncomplete_task.assert_called_once_with("list1", "t1")

    async def test_move_task(self):
        connector, client = make_connector()
        client.move_task.return_value = make_task(task_list_id="list2")

        result = await connector.call(
            "tasks_move_task", {"source_list_id": "list1", "task_id": "t1", "destination_list_id": "list2"}
        )

        client.move_task.assert_called_once_with("list1", "t1", "list2")
        assert result["task_list_id"] == "list2"


class TestNonDataclassResultPassesThroughUnchanged:
    async def test_plain_dict_result_is_not_mangled(self):
        connector, client = make_connector()
        client.complete_task.return_value = {"ok": True}

        result = await connector.call("tasks_complete_task", {"task_list_id": "list1", "task_id": "t1"})

        assert result == {"ok": True}


class TestErrorMapping:
    async def test_tasks_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_task_lists.side_effect = TasksClientError("token expired")

        with pytest.raises(RuntimeError, match="token expired"):
            await connector.call("tasks_list_task_lists", {})


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        await assert_all_tools_leave_an_audit_trail(connector, tasks_module, monkeypatch, tmp_path)
