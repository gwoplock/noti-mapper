Feature: Clearing a latch from the output that renders it
  An output is bidirectional. Resolving the PagerDuty incident is how the
  latch clears, and the daemon finds out by polling rather than by being told.

  Background:
    Given a rule "Package On Porch" from "Porch Hook" to "Porch Pager"
    And the daemon is running

  Scenario: Resolving the incident clears the latch
    When a webhook arrives
    Then "Porch Pager" eventually has an open incident
    When the incident for "Porch Pager" is resolved in PagerDuty
    Then the rule "Package On Porch" is eventually not latched

  Scenario: Resolving something already clear changes nothing
    When the incident for "Porch Pager" is resolved in PagerDuty
    And the daemon has had time to notice
    Then the rule "Package On Porch" is not latched
    And "Porch Pager" was triggered 0 times

  Scenario: The same event can latch again after being cleared
    When a webhook arrives
    Then "Porch Pager" eventually has an open incident
    When the incident for "Porch Pager" is resolved in PagerDuty
    Then the rule "Package On Porch" is eventually not latched
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    And "Porch Pager" eventually has an open incident
