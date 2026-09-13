"""The Windows autostart task definition's contract, in one place.

``installer/privacyfence-task.xml.tmpl`` is registered by
``installer/privacyfence.iss``'s ``[Code]`` section (``schtasks /create
/xml``) and is the whole of PrivacyFence's Windows autostart mechanism. It
has shipped broken more than once -- a trigger scoped to the installing
account, a missing ``version="1.2"`` that silently dropped the schema-1.2
Settings elements, a ``Principal``/``Actions`` ``id``/``Context`` pair whose
absence left the group principal bound to nothing that runs -- and every one
of those registered without complaint, so "the task exists" never caught any
of them.

This module states what the definition has to say, so two very different
tests can assert the same thing about two different documents:

* ``tests/unit/test_windows_autostart_task_template.py`` checks the template
  this repo ships, on every PR, on any OS -- a regression here is caught in
  seconds instead of by a scheduled Windows-only workflow.
* ``tests/integration/test_windows_graphical_session_autostart.py`` checks
  what Task Scheduler itself stored after a real install (``schtasks /query
  /xml``), which is the document that actually governs, and which can differ
  from the template the service was handed.
"""
from __future__ import annotations

import defusedxml.ElementTree as ET

# The task-definition schema namespace every element in a task XML lives
# under (installer/privacyfence-task.xml.tmpl's own xmlns).
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

# installer/privacyfence-task.xml.tmpl's own placeholder for the installed
# "{app}\{#AliasExeName}" path, substituted at install time.
EXEC_PATH_PLACEHOLDER = "__EXEC_PATH__"


def assert_task_xml_matches_autostart_contract(xml_text: str, *, exec_path: str) -> None:
    """Assert *xml_text* is a task definition that autostarts *exec_path*
    for whichever user signs in, unelevated, restarting it on failure.

    Every assertion below corresponds to a real, shipped bug or to a
    deliberate, documented deviation from a schema default -- see
    ``installer/privacyfence-task.xml.tmpl``'s header comment for the
    history behind each one.
    """
    root = ET.fromstring(xml_text)
    context = f"---- task XML ----\n{xml_text}"

    # Schema 1.2: MultipleInstancesPolicy and RestartOnFailure below are 1.2
    # constructs, and a definition that declares no version at all is
    # validated against 1.0, where they do not exist.
    assert root.get("version") == "1.2", f"<Task> does not declare schema version 1.2\n{context}"

    triggers = root.find(f"{TASK_NS}Triggers")
    assert triggers is not None, f"no <Triggers>\n{context}"
    logon_triggers = triggers.findall(f"{TASK_NS}LogonTrigger")
    assert len(logon_triggers) == 1, f"expected exactly one <LogonTrigger>\n{context}"
    logon_trigger = logon_triggers[0]
    enabled = logon_trigger.findtext(f"{TASK_NS}Enabled")
    assert enabled is None or enabled.strip().lower() == "true", f"<LogonTrigger> is disabled\n{context}"
    # No UserId is what makes this "any interactive logon" rather than "the
    # installing account only" -- the exact scope bug a `schtasks /create`
    # with no /RU shipped once, found by a real run of
    # windows-graphical-session.yml.
    assert logon_trigger.find(f"{TASK_NS}UserId") is None, (
        f"<LogonTrigger> is scoped to one account; it must fire for any interactive logon\n{context}"
    )

    principals = root.find(f"{TASK_NS}Principals")
    assert principals is not None, f"no <Principals>\n{context}"
    principal = principals.find(f"{TASK_NS}Principal")
    assert principal is not None, f"no <Principal>\n{context}"
    group_id = (principal.findtext(f"{TASK_NS}GroupId") or "").strip()
    assert group_id.lower().endswith("users"), (
        f"principal is {group_id!r}, not the built-in Users group -- the task would only ever run "
        f"for one account\n{context}"
    )
    # LeastPrivilege is the schema default, so a registered task may carry no
    # RunLevel element at all; what must never be true is that this daemon
    # ends up requesting an elevated token.
    run_level = (principal.findtext(f"{TASK_NS}RunLevel") or "LeastPrivilege").strip()
    assert run_level == "LeastPrivilege", f"unexpected RunLevel {run_level!r}\n{context}"

    actions = root.find(f"{TASK_NS}Actions")
    assert actions is not None, f"no <Actions>\n{context}"
    # The id/Context pair: without it the GroupId principal above is
    # registered but bound to nothing that runs -- a real shipped bug, and
    # one that `schtasks /query` alone reported as a perfectly healthy task.
    principal_id = principal.get("id")
    assert principal_id, f"<Principal> carries no id for <Actions> to name\n{context}"
    assert actions.get("Context") == principal_id, (
        f"<Actions Context={actions.get('Context')!r}> does not name the principal id "
        f"{principal_id!r}\n{context}"
    )
    command = (actions.findtext(f"{TASK_NS}Exec/{TASK_NS}Command") or "").strip().strip('"')
    assert command.lower() == exec_path.lower(), (
        f"task action runs {command!r}, not {exec_path!r}\n{context}"
    )

    settings = root.find(f"{TASK_NS}Settings")
    assert settings is not None, f"no <Settings>\n{context}"
    assert (settings.findtext(f"{TASK_NS}Enabled") or "true").strip().lower() == "true", (
        f"the task itself is registered disabled\n{context}"
    )
    # Parallel, not the schema default IgnoreNew: a GroupId trigger can
    # legitimately fire for several logged-on users at once, one instance per
    # session.
    assert (settings.findtext(f"{TASK_NS}MultipleInstancesPolicy") or "").strip() == "Parallel", context
    # Crash-restart -- the Windows analogue of the macOS LaunchAgent's
    # KeepAlive/SuccessfulExit=false and the .deb's systemd
    # Restart=on-failure (automated-test-strategy-plan.md Phase 13).
    restart = settings.find(f"{TASK_NS}RestartOnFailure")
    assert restart is not None, (
        f"no <RestartOnFailure>: the crash-restart behavior this task is supposed to carry is not "
        f"in the definition at all\n{context}"
    )
    assert (restart.findtext(f"{TASK_NS}Interval") or "").strip() == "PT1M", context
    assert (restart.findtext(f"{TASK_NS}Count") or "").strip() == "3", context
