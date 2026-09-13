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

Phase 13 added a further element that shipped looking like a fix but was
not one: ``<RestartOnFailure>`` reads as the Windows analogue of the macOS
LaunchAgent's ``KeepAlive``/``SuccessfulExit=false`` and the Linux
``.deb``'s systemd ``Restart=on-failure``, but a real ``windows-latest``
run killing a Scheduler-started daemon found that Task Scheduler logs a
killed action as a *successfully completed* task, so the setting never
engages for a crashed daemon at all -- it only ever answers a task that
fails to launch in the first place. Real crash-restart is the
``<TimeTrigger>``/``<Repetition>`` pair asserted below: an indefinitely
repeating trigger that relaunches the daemon on its own schedule, with the
already-running case a no-op thanks to the single-instance lock.

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

# What `Builtin\Users` becomes once Task Scheduler has stored the task.
BUILTIN_USERS_SID = "S-1-5-32-545"


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

    # Real crash-restart (automated-test-strategy-plan.md Phase 13): the
    # LogonTrigger above only ever fires once per sign-in, so it cannot
    # bring a daemon back after it dies mid-session -- and RestartOnFailure
    # (asserted below) was measured on a real windows-latest runner not to
    # cover that case at all (Task Scheduler logs a killed action as a
    # successfully completed task). A TimeTrigger with an indefinite
    # Repetition is what actually relaunches a dead daemon: no default
    # fallback on Repetition/Interval or StartBoundary below, since a
    # missing or misconfigured element there silently means "no
    # crash-restart," exactly the failure mode this phase exists to close.
    time_triggers = triggers.findall(f"{TASK_NS}TimeTrigger")
    assert len(time_triggers) == 1, f"expected exactly one <TimeTrigger>\n{context}"
    time_trigger = time_triggers[0]
    # Same "None means the schema default of true" fallback as LogonTrigger's
    # own Enabled check above, not a looser standard invented for this
    # element: a real windows-latest run confirmed Task Scheduler stores
    # neither trigger's <Enabled> at all when it is true, the same way it
    # normalizes away any other schema-default value.
    time_trigger_enabled = time_trigger.findtext(f"{TASK_NS}Enabled")
    assert time_trigger_enabled is None or time_trigger_enabled.strip().lower() == "true", (
        f"<TimeTrigger> is disabled\n{context}"
    )
    assert (time_trigger.findtext(f"{TASK_NS}StartBoundary") or "").strip(), (
        f"<TimeTrigger> has no <StartBoundary>\n{context}"
    )
    repetition = time_trigger.find(f"{TASK_NS}Repetition")
    assert repetition is not None, f"<TimeTrigger> has no <Repetition>: it will only ever fire once\n{context}"
    assert (repetition.findtext(f"{TASK_NS}Interval") or "").strip() == "PT5M", (
        f"<TimeTrigger><Repetition><Interval> is not PT5M\n{context}"
    )

    principals = root.find(f"{TASK_NS}Principals")
    assert principals is not None, f"no <Principals>\n{context}"
    principal = principals.find(f"{TASK_NS}Principal")
    assert principal is not None, f"no <Principal>\n{context}"
    group_id = (principal.findtext(f"{TASK_NS}GroupId") or "").strip()
    # Task Scheduler stores the principal as the group's SID, not the name
    # the template writes, so both spellings have to be accepted: this same
    # contract is asserted against the template on one side and against the
    # registered definition on the other.
    assert group_id.lower().endswith("users") or group_id.upper() == BUILTIN_USERS_SID, (
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
    # Both of these default to true, and both defaults are wrong for this
    # task: on a laptop they mean "do not start PrivacyFence at sign-in while
    # on battery" and "stop it when the user unplugs". Asserted explicitly
    # (no `or "true"` fallback) because their absence is the bug -- that is
    # exactly how they shipped, unnoticed, until a test read the definition
    # back out of Task Scheduler rather than out of the template.
    assert (settings.findtext(f"{TASK_NS}DisallowStartIfOnBatteries") or "").strip() == "false", (
        f"DisallowStartIfOnBatteries is not false: autostart would not run on battery power\n{context}"
    )
    assert (settings.findtext(f"{TASK_NS}StopIfGoingOnBatteries") or "").strip() == "false", (
        f"StopIfGoingOnBatteries is not false: the daemon would be stopped when the machine unplugs\n{context}"
    )
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
