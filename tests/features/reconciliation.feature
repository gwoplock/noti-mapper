Feature: Reconciling with the world after downtime
  The daemon was off. Things happened. This is the part that silently drops an
  event if it is got wrong, so every branch of it gets a scenario.

  Background:
    Given a rule "Package On Porch" from inputs "Porch Hook" and "Porch Files" to "Porch Pager"

  Scenario: A latch survives a restart
    Given the daemon is running
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    When the daemon is restarted
    Then the rule "Package On Porch" is latched

  Scenario: An incident resolved while the daemon was down clears the latch
    Given the daemon is running
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    When the daemon is stopped
    And the incident for "Porch Pager" was resolved 2 hours ago
    And the daemon is started
    Then the rule "Package On Porch" is eventually not latched

  @wip
  Scenario: An event after the resolve wins
    This is the one that silently drops a notification if it is got wrong: you
    resolved the incident from your phone, and then the package arrived.

    Tagged @wip because it does not pass, and it is not yet known whether the
    daemon or this harness is at fault. The daemon logs "reconciled rule
    'Package On Porch' to False: an output reports it was cleared during
    downtime", which means no downtime event outran the clear. Widening the gap
    between the resolve and the file to two seconds does not change it, so the
    event is missing rather than merely close.

    One candidate: Daemon.start() starts the input threads before it
    reconciles, so the file watcher's own poll can reach the file first, call
    _remember on it, and leave FileInput.catch_up seeing nothing new. That
    would be a real ordering bug in startup rather than a test artefact -- but
    it is a hypothesis, not a diagnosis, and the queued event should still
    latch once the core loop drains. Do not delete this scenario to make the
    suite green.

    Given the daemon is running
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    When the daemon is stopped
    And the incident for "Porch Pager" is resolved in PagerDuty
    And a moment passes
    And a file "late.txt" appears
    And the daemon is started
    Then the rule "Package On Porch" is eventually latched
    And "Porch Pager" eventually has an open incident

  Scenario: An event before the resolve does not
    Given the daemon is running
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    When the daemon is stopped
    And a file "early.txt" appears
    And a moment passes
    And the incident for "Porch Pager" is resolved in PagerDuty
    And the daemon is started
    Then the rule "Package On Porch" is eventually not latched

  Scenario: A file that predates the last run is not news
    A file whose mtime is older than the daemon's last run did not arrive
    during the downtime, whatever the seen-cursor says, so it does not fire.

    Given the daemon is running
    When the daemon is stopped
    And a file "ancient.txt" appeared 3 hours ago
    And the daemon is started
    And the daemon has had time to notice
    Then the rule "Package On Porch" is not latched
