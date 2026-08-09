Feature: Refusing to run on a configuration that is wrong
  Every problem is reported in one pass, and a rule that leaves the
  configuration takes its latch out of service without deleting it.

  Scenario: validate accepts a good configuration
    Given a rule "Package On Porch" from "Porch Hook" to "Porch Pager"
    When "validate" is run
    Then it exits successfully
    And the output mentions "configuration is valid"

  Scenario: validate names the plugin and the path to it
    Given a rule "Package On Porch" from "Porch Hook" to "Porch Pager"
    And the instance "Porch Pager" uses plugin "pagerduty"
    When "validate" is run
    Then it exits with a configuration error
    And the output mentions "unknown plugin 'pagerduty'"
    And the output mentions "instances → 'Porch Pager' → plugin"

  Scenario: validate reports every problem at once
    Given a rule "Package On Porch" from "Porch Hook" to "Nowhere"
    And the instance "Porch Pager" uses plugin "pagerduty"
    When "validate" is run
    Then it exits with a configuration error
    And the output mentions "2 configuration errors"

  Scenario: A rule that leaves the configuration orphans its latch
    Given a rule "Package On Porch" from "Porch Hook" to "Porch Pager"
    And the daemon is running
    When a webhook arrives
    Then the rule "Package On Porch" is latched
    When the rule "Package On Porch" is removed from the configuration
    And the daemon is reloaded
    Then "Porch Pager" eventually has no open incident
    And "status" reports "Package On Porch" as orphaned

  Scenario: purge clears an orphaned latch only when asked
    Given a rule "Package On Porch" from "Porch Hook" to "Porch Pager"
    And the daemon is running
    When a webhook arrives
    And the rule "Package On Porch" is removed from the configuration
    And the daemon is reloaded
    And the daemon is stopped
    And "purge --yes" is run
    Then it exits successfully
    And the output mentions "purged 1 rule"
