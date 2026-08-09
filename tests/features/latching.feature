Feature: Latching an event until somebody acknowledges it
  An input fires, every rule listing that input latches, and every output
  those rules name is driven. The latch is the product; everything else in
  this file exists to make it observable from outside the daemon.

  Background:
    Given a rule "Package On Porch" from "Porch Hook" to "Porch Pager"
    And the daemon is running

  Scenario: A webhook latches the rule and opens an incident
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    And "Porch Pager" eventually has an open incident

  Scenario: The incident carries what caused it
    When a webhook arrives with body '{"carrier": "ups"}'
    Then "Porch Pager" eventually has an open incident
    And the incident for "Porch Pager" was caused by "Porch Hook"

  Scenario: A file appearing latches the rule too
    Given a rule "Package On Porch" from "Porch Files" to "Porch Pager"
    And the daemon is restarted
    When a file "package.txt" appears
    Then the rule "Package On Porch" is eventually latched

  Scenario: Re-triggering an already-set latch does not alert twice
    When a webhook arrives
    Then "Porch Pager" eventually has an open incident
    When a webhook arrives
    And a webhook arrives
    Then "Porch Pager" was triggered 1 time
    And the rule "Package On Porch" is latched

  Scenario: An unauthenticated webhook latches nothing
    When a webhook arrives with no token
    Then the webhook was refused
    And the rule "Package On Porch" is not latched
