Feature: A failing output cannot veto a state change
  If PagerDuty is unreachable the latch still sets. The push retries with
  backoff and applies whatever the latch says when it finally lands, never the
  value that failed.

  Background:
    Given a rule "Package On Porch" from "Porch Hook" to "Porch Pager"
    And the daemon is running

  Scenario: The latch sets even though the push fails
    Given PagerDuty is returning 503
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    And "Porch Pager" has no open incident
    And the retry queue eventually holds a push for "Porch Pager"

  Scenario: The push lands once PagerDuty recovers
    Given PagerDuty is returning 503
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    When PagerDuty recovers
    Then "Porch Pager" eventually has an open incident
    And the retry queue is eventually empty

  Scenario: A retry applies current state, not the value that failed
    Given PagerDuty is returning 503
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    When the incident for "Porch Pager" is resolved in PagerDuty
    And the daemon has had time to notice
    And PagerDuty recovers
    Then the rule "Package On Porch" is eventually not latched
    And "Porch Pager" was triggered 0 times
