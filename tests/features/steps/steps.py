"""Step definitions.

Every step goes through the world rather than reaching into the daemon, so the
same scenario is evidence about the local subprocess and about the packaged
container. Nothing here imports noti_mapper: these steps only know what an
operator knows -- HTTP, files, the CLI, and what PagerDuty was told.
"""

import datetime
import json
import time
from typing import Any

from behave import given, then, when

from tests.features.support import world as support

# The pollers in play have their own intervals; a step that says "eventually"
# is waiting for one of them rather than for the daemon to be slow.
NOTICE_SECONDS = 8.0


# -- configuration ------------------------------------------------------------


def _configure(context: Any) -> None:
    support.write_configuration(context.daemon, rules=context.rules)


@given('a rule "{rule}" from "{source}" to "{target}"')
def step_rule(context: Any, rule: str, source: str, target: str) -> None:
    context.rules[rule] = {"inputs": [source], "outputs": [target]}
    _drop_default(context, rule)
    _configure(context)


@given('a rule "{rule}" from "{source}" to outputs "{first}" and "{second}"')
def step_rule_two_outputs(context: Any, rule: str, source: str, first: str, second: str) -> None:
    context.rules[rule] = {"inputs": [source], "outputs": [first, second]}
    _drop_default(context, rule)
    _configure(context)


@given('a rule "{rule}" from inputs "{first}" and "{second}" to "{target}"')
def step_rule_two_inputs(context: Any, rule: str, first: str, second: str, target: str) -> None:
    context.rules[rule] = {"inputs": [first, second], "outputs": [target]}
    _drop_default(context, rule)
    _configure(context)


def _drop_default(context: Any, rule: str) -> None:
    """The default rule from before_scenario goes as soon as a scenario names one."""
    if rule != "Package On Porch":
        context.rules.pop("Package On Porch", None)


@given('the instance "{instance}" uses plugin "{plugin}"')
def step_break_plugin(context: Any, instance: str, plugin: str) -> None:
    document = json.loads(context.daemon.read_config("10-instances.json"))
    document["instances"][instance]["plugin"] = plugin
    context.daemon.write_config("10-instances.json", json.dumps(document, indent=2))


@when('the rule "{rule}" is removed from the configuration')
def step_remove_rule(context: Any, rule: str) -> None:
    context.rules.pop(rule, None)
    context.daemon.write_config("20-rules.json", json.dumps({"rules": context.rules}, indent=2))


# -- the daemon ---------------------------------------------------------------


@given("the daemon is running")
@when("the daemon is started")
def step_start(context: Any) -> None:
    """Start it if it is not already up.

    Deliberately not "restart it if the configuration changed": a scenario that
    rewires the rules after a Background has started the daemon has to say so,
    because whether the daemon saw the change is the thing under test.
    """
    context.daemon.start()


@when("the daemon is stopped")
def step_stop(context: Any) -> None:
    context.daemon.stop()


@given("the daemon is restarted")
@when("the daemon is restarted")
def step_restart(context: Any) -> None:
    context.daemon.stop()
    context.daemon.start()


@when("the daemon is reloaded")
def step_reload(context: Any) -> None:
    context.daemon.reload()
    time.sleep(1.0)


@when("a moment passes")
def step_moment(context: Any) -> None:
    """Enough for a file mtime to sort strictly after what came before it.

    Reconciliation compares an event's timestamp against an output's clear
    time, so a scenario about which of the two happened first has to actually
    put them in that order rather than in the same microsecond.
    """
    del context
    time.sleep(0.3)


@when("the daemon has had time to notice")
def step_wait_for_poll(context: Any) -> None:
    del context
    time.sleep(NOTICE_SECONDS)


# -- inputs -------------------------------------------------------------------


@when("a webhook arrives")
def step_webhook(context: Any) -> None:
    context.webhook_status = context.world.post_webhook()


@when("a webhook arrives with body '{body}'")
def step_webhook_body(context: Any, body: str) -> None:
    context.webhook_status = context.world.post_webhook(body=body)


@when("a webhook arrives with no token")
def step_webhook_no_token(context: Any) -> None:
    context.webhook_status = context.world.post_webhook(token=None)


@then("the webhook was refused")
def step_webhook_refused(context: Any) -> None:
    assert context.webhook_status == 401, f"expected 401, got {context.webhook_status}"


@when('a file "{name}" appears')
def step_file(context: Any, name: str) -> None:
    context.world.drop_file(name)


@when('a file "{name}" appeared {hours:d} hour ago')
@when('a file "{name}" appeared {hours:d} hours ago')
def step_old_file(context: Any, name: str, hours: int) -> None:
    context.world.drop_file(name, age_hours=float(hours))


# -- latches ------------------------------------------------------------------


@then('the rule "{rule}" is latched')
def step_latched(context: Any, rule: str) -> None:
    assert context.world.latch_is_set(rule), _why(context, rule, "set")


@then('the rule "{rule}" is not latched')
def step_not_latched(context: Any, rule: str) -> None:
    assert not context.world.latch_is_set(rule), _why(context, rule, "clear")


@then('the rule "{rule}" is eventually latched')
def step_eventually_latched(context: Any, rule: str) -> None:
    try:
        context.world.eventually(
            lambda: context.world.latch_is_set(rule), what=f"rule {rule!r} to latch"
        )
    except AssertionError as error:
        raise AssertionError(f"{error}\n\n{_why(context, rule, 'set')}") from error


@then('the rule "{rule}" is eventually not latched')
def step_eventually_cleared(context: Any, rule: str) -> None:
    try:
        context.world.eventually(
            lambda: not context.world.latch_is_set(rule), what=f"rule {rule!r} to clear"
        )
    except AssertionError as error:
        raise AssertionError(f"{error}\n\n{_why(context, rule, 'clear')}") from error


def _why(context: Any, rule: str, expected: str) -> str:
    return (
        f"expected rule {rule!r} to be {expected}.\n\n"
        f"status:\n{context.world.status_text()}\n"
        f"daemon log:\n{context.daemon.logs()}"
    )


# -- what PagerDuty saw -------------------------------------------------------


@then('"{instance}" eventually has an open incident')
def step_incident_open(context: Any, instance: str) -> None:
    key = support.dedup_key_for(instance)
    context.world.eventually(
        lambda: context.stub.is_open(key), what=f"an open incident for {instance!r}"
    )


@then('"{instance}" eventually has no open incident')
def step_incident_closed(context: Any, instance: str) -> None:
    key = support.dedup_key_for(instance)
    context.world.eventually(
        lambda: not context.stub.is_open(key), what=f"{instance!r} to be resolved"
    )


@then('"{instance}" has no open incident')
def step_incident_absent(context: Any, instance: str) -> None:
    key = support.dedup_key_for(instance)
    assert not context.stub.is_open(key), f"{instance!r} unexpectedly has an open incident"


@then('"{instance}" was triggered {count:d} time')
@then('"{instance}" was triggered {count:d} times')
def step_trigger_count(context: Any, instance: str, count: int) -> None:
    key = support.dedup_key_for(instance)
    actual = context.stub.event_count(key, "trigger")
    assert actual == count, f"expected {count} triggers for {instance!r}, saw {actual}"


@then('the incident for "{instance}" was caused by "{cause}"')
def step_incident_cause(context: Any, instance: str, cause: str) -> None:
    incident = context.stub.incident(support.dedup_key_for(instance))
    assert incident is not None, f"no incident for {instance!r}"
    assert (
        incident.custom_details.get("caused_by") == cause
    ), f"expected caused_by {cause!r}, got {incident.custom_details!r}"


@when('the incident for "{instance}" is resolved in PagerDuty')
def step_resolve(context: Any, instance: str) -> None:
    context.stub.resolve_externally(support.dedup_key_for(instance), at=support.now())


@when('the incident for "{instance}" was resolved {hours:d} hours ago')
def step_resolved_earlier(context: Any, instance: str, hours: int) -> None:
    when = support.now() - datetime.timedelta(hours=hours)
    context.stub.resolve_externally(support.dedup_key_for(instance), at=when)


@given("PagerDuty is returning {status:d}")
def step_pagerduty_failing(context: Any, status: int) -> None:
    context.stub.fail_events_with(status)


@when("PagerDuty recovers")
def step_pagerduty_recovers(context: Any) -> None:
    context.stub.fail_events_with(None)


# -- the retry queue ----------------------------------------------------------


@then('the retry queue eventually holds a push for "{instance}"')
def step_retry_present(context: Any, instance: str) -> None:
    context.world.eventually(
        lambda: f"{instance!r}" in _pending_section(context.world.status_text()),
        what=f"a pending push for {instance!r}",
    )


@then("the retry queue is eventually empty")
def step_retry_empty(context: Any) -> None:
    context.world.eventually(
        lambda: "(none)" in _pending_section(context.world.status_text()),
        what="the retry queue to drain",
    )


def _pending_section(status: str) -> str:
    _, _, rest = status.partition("Pending retries")
    section, _, _ = rest.partition("\n\nPlugin health")
    return section


# -- the command line ---------------------------------------------------------


@when('"{command}" is run')
def step_cli(context: Any, command: str) -> None:
    context.cli_result = context.daemon.cli(*command.split())


@then("it exits successfully")
def step_cli_ok(context: Any) -> None:
    result = context.cli_result
    assert result.returncode == 0, f"exit {result.returncode}:\n{result.stdout}{result.stderr}"


@then("it exits with a configuration error")
def step_cli_config_error(context: Any) -> None:
    result = context.cli_result
    assert result.returncode == 2, f"expected exit 2, got {result.returncode}:\n{result.stderr}"


@then('the output mentions "{text}"')
def step_cli_mentions(context: Any, text: str) -> None:
    result = context.cli_result
    combined = f"{result.stdout}{result.stderr}"
    assert text in combined, f"expected {text!r} in:\n{combined}"


@then('"status" reports "{rule}" as orphaned')
def step_status_orphaned(context: Any, rule: str) -> None:
    status = context.world.status_text()
    assert "Orphaned rules" in status, f"no orphan section in:\n{status}"
    assert (
        f"{rule!r}" in status.partition("Orphaned rules")[2]
    ), f"expected {rule!r} to be listed as orphaned:\n{status}"
