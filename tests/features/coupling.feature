Feature: Two outputs on one rule are coupled
  Clearing from either output clears the rule, so both go quiet together.
  A user who wants them to clear independently writes two rules, and this is
  the difference spelled out.

  Scenario: Clearing from one output silences the other
    Given a rule "Package On Porch" from "Porch Hook" to outputs "Porch Pager" and "Hall Pager"
    And the daemon is running
    When a webhook arrives
    Then "Porch Pager" eventually has an open incident
    And "Hall Pager" eventually has an open incident
    When the incident for "Hall Pager" is resolved in PagerDuty
    Then the rule "Package On Porch" is eventually not latched
    And "Porch Pager" eventually has no open incident

  Scenario: Two rules clear independently
    Given a rule "Porch Alert" from "Porch Hook" to "Porch Pager"
    And a rule "Hall Alert" from "Porch Hook" to "Hall Pager"
    And the daemon is running
    When a webhook arrives
    Then the rule "Porch Alert" is latched
    And the rule "Hall Alert" is latched
    When the incident for "Hall Pager" is resolved in PagerDuty
    Then the rule "Hall Alert" is eventually not latched
    And the rule "Porch Alert" is latched
    And "Porch Pager" eventually has an open incident
