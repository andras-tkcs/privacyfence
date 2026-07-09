"""Tests for TasksClient's parsing/normalization logic and the multi-call
operations (update_task's partial-field preservation, move_task's
insert-then-delete sequencing).
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from privacyfence.tasks_client import Task, TaskList, TasksClient, TasksClientError
from googleapiclient.errors import HttpError


def make_client(service: MagicMock) -> TasksClient:
    client = TasksClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.service = service
    return client


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


# ---------------------------------------------------------------------------- #
# _parse_task
# ---------------------------------------------------------------------------- #

class TestParseTask:
    def test_full_task_normalized(self):
        raw = {
            "id": "t1", "title": "Buy milk", "notes": "2%", "due": "2024-01-01",
            "status": "needsAction", "completed": "", "updated": "u", "position": "p", "parent": "parent1",
        }
        task = TasksClient._parse_task(raw, "list1")
        assert task == Task(
            id="t1", task_list_id="list1", title="Buy milk", notes="2%", due="2024-01-01",
            status="needsAction", completed="", updated="u", position="p", parent="parent1",
        )

    def test_missing_fields_default_sensibly(self):
        task = TasksClient._parse_task({}, "list1")
        assert task.status == "needsAction"
        assert task.title == ""

    def test_short_summary_reflects_status(self):
        done = TasksClient._parse_task({"title": "X", "status": "completed"}, "l")
        todo = TasksClient._parse_task({"title": "X", "status": "needsAction"}, "l")
        assert done.short_summary() == "X (done)"
        assert todo.short_summary() == "X (todo)"


# ---------------------------------------------------------------------------- #
# list_task_lists / list_tasks / get_task
# ---------------------------------------------------------------------------- #

class TestListTaskLists:
    def test_maps_response(self):
        service = MagicMock()
        service.tasklists.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "l1", "title": "My List", "updated": "u"}]
        }
        client = make_client(service)
        assert client.list_task_lists() == [TaskList(id="l1", title="My List", updated="u")]

    def test_http_error_becomes_tasks_client_error(self):
        service = MagicMock()
        service.tasklists.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(TasksClientError, match="list_task_lists failed"):
            client.list_task_lists()


class TestGetTaskList:
    def test_requires_task_list_id(self):
        client = make_client(MagicMock())
        with pytest.raises(TasksClientError, match="requires a task_list_id"):
            client.get_task_list("")

    def test_maps_response(self):
        service = MagicMock()
        service.tasklists.return_value.get.return_value.execute.return_value = {
            "id": "l1", "title": "Groceries", "updated": "u",
        }
        client = make_client(service)

        result = client.get_task_list("l1")

        assert result == TaskList(id="l1", title="Groceries", updated="u")
        service.tasklists.return_value.get.assert_called_once_with(tasklist="l1")

    def test_http_error_becomes_tasks_client_error(self):
        service = MagicMock()
        service.tasklists.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(TasksClientError, match="get_task_list\\(l1\\) failed"):
            client.get_task_list("l1")


class TestListTasks:
    def test_requires_task_list_id(self):
        client = make_client(MagicMock())
        with pytest.raises(TasksClientError, match="requires a task_list_id"):
            client.list_tasks("")

    def test_show_completed_flag_passed_through(self):
        service = MagicMock()
        service.tasks.return_value.list.return_value.execute.return_value = {"items": []}
        client = make_client(service)
        client.list_tasks("list1", show_completed=True)
        assert service.tasks.return_value.list.call_args.kwargs["showCompleted"] is True

    def test_maps_response_with_list_id_attached(self):
        service = MagicMock()
        service.tasks.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "t1", "title": "Task"}]
        }
        client = make_client(service)
        tasks = client.list_tasks("list1")
        assert tasks[0].task_list_id == "list1"

    def test_http_error_becomes_tasks_client_error(self):
        service = MagicMock()
        service.tasks.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(TasksClientError, match="list_tasks"):
            client.list_tasks("list1")


class TestGetTask:
    def test_requires_both_ids(self):
        client = make_client(MagicMock())
        with pytest.raises(TasksClientError, match="requires task_list_id and task_id"):
            client.get_task("", "t1")
        with pytest.raises(TasksClientError, match="requires task_list_id and task_id"):
            client.get_task("l1", "")


# ---------------------------------------------------------------------------- #
# create_task
# ---------------------------------------------------------------------------- #

class TestCreateTask:
    def test_requires_task_list_id_and_title(self):
        client = make_client(MagicMock())
        with pytest.raises(TasksClientError, match="requires task_list_id and title"):
            client.create_task("", "title")
        with pytest.raises(TasksClientError, match="requires task_list_id and title"):
            client.create_task("l1", "")

    def test_notes_and_due_included_only_when_given(self):
        service = MagicMock()
        service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t1", "title": "T"}
        client = make_client(service)
        client.create_task("l1", "T")
        body = service.tasks.return_value.insert.call_args.kwargs["body"]
        assert body == {"title": "T"}

    def test_notes_and_due_included_when_given(self):
        service = MagicMock()
        service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t1", "title": "T"}
        client = make_client(service)
        client.create_task("l1", "T", notes="n", due="2024-01-01")
        body = service.tasks.return_value.insert.call_args.kwargs["body"]
        assert body == {"title": "T", "notes": "n", "due": "2024-01-01"}

    def test_http_error_becomes_tasks_client_error(self):
        service = MagicMock()
        service.tasks.return_value.insert.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(TasksClientError, match="create_task"):
            client.create_task("l1", "T")


# ---------------------------------------------------------------------------- #
# update_task: partial-field preservation from the existing task
# ---------------------------------------------------------------------------- #

class TestUpdateTask:
    def test_unspecified_fields_preserved_from_existing_task(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {
            "id": "t1", "title": "Old title", "notes": "old notes", "due": "2024-01-01",
        }
        service.tasks.return_value.update.return_value.execute.return_value = {"id": "t1", "title": "New title"}
        client = make_client(service)

        client.update_task("l1", "t1", title="New title")

        body = service.tasks.return_value.update.call_args.kwargs["body"]
        assert body["title"] == "New title"
        assert body["notes"] == "old notes"
        assert body["due"] == "2024-01-01"

    def test_due_can_be_explicitly_cleared_by_passing_none_is_not_possible_uses_existing(self):
        # due=None (the default) means "don't touch due" -> falls back to
        # existing.due if present.
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {"id": "t1", "title": "T", "due": "2024-06-01"}
        service.tasks.return_value.update.return_value.execute.return_value = {"id": "t1"}
        client = make_client(service)

        client.update_task("l1", "t1", title="T2")

        body = service.tasks.return_value.update.call_args.kwargs["body"]
        assert body["due"] == "2024-06-01"

    def test_no_due_on_existing_and_none_given_omits_due(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {"id": "t1", "title": "T"}
        service.tasks.return_value.update.return_value.execute.return_value = {"id": "t1"}
        client = make_client(service)

        client.update_task("l1", "t1", title="T2")

        body = service.tasks.return_value.update.call_args.kwargs["body"]
        assert "due" not in body

    def test_get_http_error_propagates_as_get_task_error(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(TasksClientError, match="get_task"):
            client.update_task("l1", "t1", title="x")

    def test_update_http_error_becomes_tasks_client_error(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {"id": "t1", "title": "T"}
        service.tasks.return_value.update.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(TasksClientError, match="update_task"):
            client.update_task("l1", "t1", title="x")


# ---------------------------------------------------------------------------- #
# complete_task / uncomplete_task
# ---------------------------------------------------------------------------- #

class TestCompleteUncompleteTask:
    def test_complete_task_sets_status_completed(self):
        service = MagicMock()
        service.tasks.return_value.patch.return_value.execute.return_value = {"id": "t1", "status": "completed"}
        client = make_client(service)
        task = client.complete_task("l1", "t1")
        assert task.status == "completed"
        assert service.tasks.return_value.patch.call_args.kwargs["body"] == {"status": "completed"}

    def test_uncomplete_task_clears_completed_timestamp(self):
        service = MagicMock()
        service.tasks.return_value.patch.return_value.execute.return_value = {"id": "t1", "status": "needsAction"}
        client = make_client(service)
        client.uncomplete_task("l1", "t1")
        assert service.tasks.return_value.patch.call_args.kwargs["body"] == {
            "status": "needsAction", "completed": None,
        }

    def test_complete_task_http_error_becomes_tasks_client_error(self):
        service = MagicMock()
        service.tasks.return_value.patch.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(TasksClientError, match="complete_task"):
            client.complete_task("l1", "t1")


# ---------------------------------------------------------------------------- #
# move_task: insert into destination, then delete from source
# ---------------------------------------------------------------------------- #

class TestMoveTask:
    def test_requires_all_three_ids(self):
        client = make_client(MagicMock())
        with pytest.raises(TasksClientError, match="requires source_list_id"):
            client.move_task("", "t1", "dest")
        with pytest.raises(TasksClientError, match="requires source_list_id"):
            client.move_task("src", "", "dest")
        with pytest.raises(TasksClientError, match="requires source_list_id"):
            client.move_task("src", "t1", "")

    def test_inserts_into_destination_then_deletes_from_source(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {
            "id": "t1", "title": "T", "notes": "n", "due": "2024-01-01",
        }
        service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t2", "title": "T"}
        client = make_client(service)

        result = client.move_task("src", "t1", "dest")

        insert_kwargs = service.tasks.return_value.insert.call_args.kwargs
        assert insert_kwargs["tasklist"] == "dest"
        assert insert_kwargs["body"] == {"title": "T", "notes": "n", "due": "2024-01-01"}
        delete_kwargs = service.tasks.return_value.delete.call_args.kwargs
        assert delete_kwargs == {"tasklist": "src", "task": "t1"}
        assert result.task_list_id == "dest"

    def test_notes_and_due_omitted_from_new_body_when_absent(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {"id": "t1", "title": "T"}
        service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t2", "title": "T"}
        client = make_client(service)

        client.move_task("src", "t1", "dest")

        assert service.tasks.return_value.insert.call_args.kwargs["body"] == {"title": "T"}

    def test_insert_failure_becomes_tasks_client_error_and_skips_delete(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {"id": "t1", "title": "T"}
        service.tasks.return_value.insert.return_value.execute.side_effect = http_error(400)
        client = make_client(service)

        with pytest.raises(TasksClientError, match="move_task insert"):
            client.move_task("src", "t1", "dest")
        service.tasks.return_value.delete.assert_not_called()

    def test_delete_failure_becomes_tasks_client_error(self):
        service = MagicMock()
        service.tasks.return_value.get.return_value.execute.return_value = {"id": "t1", "title": "T"}
        service.tasks.return_value.insert.return_value.execute.return_value = {"id": "t2", "title": "T"}
        service.tasks.return_value.delete.return_value.execute.side_effect = http_error(400)
        client = make_client(service)

        with pytest.raises(TasksClientError, match="move_task delete"):
            client.move_task("src", "t1", "dest")


# ---------------------------------------------------------------------------- #
# _get_service: must not share one service (and its underlying httplib2
# transport) across threads, since concurrent requests dispatched via
# asyncio.to_thread corrupt a shared connection (SSL: WRONG_VERSION_NUMBER).
# ---------------------------------------------------------------------------- #

class TestServiceIsThreadLocal:
    def test_each_thread_gets_its_own_service_instance(self):
        client = TasksClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.tasks_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()

            services: dict[int, object] = {}

            def worker(idx: int) -> None:
                services[idx] = client._get_service()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len({id(s) for s in services.values()}) == 5
            assert mock_build.call_count == 5

    def test_same_thread_reuses_cached_service(self):
        client = TasksClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.tasks_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()

            first = client._get_service()
            second = client._get_service()

            assert first is second
            assert mock_build.call_count == 1
