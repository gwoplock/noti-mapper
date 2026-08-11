# Configuration reference

Configuration is a set of JSON files in `/etc/noti-mapper.d/`, read in lexical
order and merged. Every file is an object with one or more of the top-level
keys `instances`, `rules`, and `daemon`.

Check your work before restarting:

```bash
noti-mapper validate
```

Every problem is reported in one pass, with the file it is in and the path
through that file to the thing that is wrong:

```
noti-mapper: 2 configuration errors:
  /etc/noti-mapper.d/10-instances.json: instances → 'Porch Mail' → plugin:
      instance 'Porch Mail' refers to unknown plugin 'imap'. Plugins that loaded: ...
  /etc/noti-mapper.d/20-rules.json: rules → 'Package On Porch' → inputs[0]:
      rule 'Package On Porch' references unknown instance 'Porch Mial'; did you mean 'Porch Mail'?
```

You should never have to fix errors one restart at a time. A path rather than
a line number, because the line moves when you reformat the file and the path
does not.

## Names

Every identifier here is a name you typed. There are no numeric IDs, no UUIDs,
and no generated keys anywhere in the configuration surface or the CLI. Names
appear verbatim in logs, in `noti-mapper status`, and in error messages.

- Allowed characters: letters, digits, spaces, hyphens, underscores, periods.
  Everything else is rejected — quotes and slashes especially, because names
  flow into command arguments and log lines.
- Maximum 128 characters. Leading and trailing whitespace is stripped; interior
  whitespace is kept.
- Stored as you typed them, compared case-insensitively. `Porch Mail` and
  `porch mail` in the same namespace is an error, not two objects.
- Instance names and rule names are separate namespaces. A rule may share a
  name with an instance.

Spaces are allowed because they read well. They need quoting on the command
line:

```bash
noti-mapper rename "Package On Porch" "Porch Package"
```

## Rules

A rule is a named mapping from a set of input instances to a set of output
instances. The latch belongs to the rule.

```json
{
  "rules": {
    "Package On Porch": {
      "inputs": ["Porch Mail"],
      "outputs": ["Porch Lamp", "Porch Pager"]
    }
  }
}
```

### Renaming a rule is not free — read this before you edit one

**The latch is keyed on the rule name.** Renaming a rule in a configuration
file is indistinguishable, from the daemon's point of view, from deleting one
rule and creating a different one: the old latch orphans and the new rule
starts cleared. If the rule was set, whatever it was telling you about is
silently forgotten.

Migrate the latch first:

```bash
systemctl stop noti-mapper
noti-mapper rename "Package On Porch" "Porch Package"
# now edit /etc/noti-mapper.d/ to match
noti-mapper validate
systemctl start noti-mapper
```

Renaming an *instance* is cheaper — instances hold no latch — but it still
orphans that instance's plugin scratch storage, which for the IMAP reader means
its UID cursor. The next start re-baselines at the current highest UID without
emitting, so you lose nothing except the ability to catch up on anything that
arrived in between.

### Fields

| Field     | Required | Meaning                                                 |
| --------- | -------- | ------------------------------------------------------- |
| `inputs`  | yes      | Non-empty. An event from any of these sets the latch.    |
| `outputs` | yes      | Non-empty. All of these render it, and any can clear it. |
| `enabled` | no       | Defaults to `true`.                                      |

A rule may not list the same instance in both `inputs` and `outputs`; that
would make it latch itself.

## Rule semantics, worked through

These four behaviours follow from "the latch belongs to the rule". They are the
ones that surprise people, so here they are with the outcome spelled out.

### Two outputs on one rule are coupled

```json
{
  "rules": {
    "Package On Porch": {
      "inputs": ["Porch Mail"],
      "outputs": ["Porch Lamp", "Porch Pager"]
    }
  }
}
```

| Step                                | Latch   | Porch Lamp | Porch Pager |
| ----------------------------------- | ------- | ---------- | ----------- |
| start                               | clear   | off        | resolved    |
| mail arrives                        | **set** | **on**     | **open**    |
| you resolve the PagerDuty incident  | clear   | **off**    | resolved    |

Clearing from *either* output clears the rule, so both go quiet. That is
usually what you want: acknowledging the alert acknowledges the alert.

If you want to turn the lamp off without resolving the incident, write two
rules instead:

```json
{
  "rules": {
    "Package On Porch Lamp":  { "inputs": ["Porch Mail"], "outputs": ["Porch Lamp"] },
    "Package On Porch Pager": { "inputs": ["Porch Mail"], "outputs": ["Porch Pager"] }
  }
}
```

| Step                    | Lamp rule | Pager rule | Porch Lamp | Porch Pager |
| ----------------------- | --------- | ---------- | ---------- | ----------- |
| mail arrives            | **set**   | **set**    | **on**     | **open**    |
| you turn the lamp off   | clear     | set        | off        | **open**    |

The coupling is a configuration decision rather than a behaviour baked into the
daemon. That is the main reason latches are keyed on the rule rather than on
the input.

### The same input in two rules latches twice

Both rules above list `Porch Mail`. One event sets both latches, and they clear
independently. Nothing is shared between them except the input that fired.

### An output shared by two rules is the OR of them

```json
{
  "rules": {
    "Package On Porch": { "inputs": ["Porch Mail"],  "outputs": ["Hall Lamp"] },
    "Back Door Open":   { "inputs": ["Door Sensor"], "outputs": ["Hall Lamp"] }
  }
}
```

| Step                      | Package latch | Door latch | Hall Lamp |
| ------------------------- | ------------- | ---------- | --------- |
| mail arrives              | **set**       | clear      | **on**    |
| the door opens            | set           | **set**    | on        |
| you turn the Hall Lamp off| **clear**     | **clear**  | **off**   |

Note the last row: an unlatch from `Hall Lamp` clears *every* rule that lists it
as an output, not just one of them. There is no way to say "clear only the door
one" through an output that both rules share — that is what separate outputs are
for.

### Re-triggering is not a no-op

A second event against an already-set latch does not produce an output
transition — the lamp is already on, and turning it on again would be noise.
It does bump a counter and move the timestamp, and outputs are told both, so a
PagerDuty incident can say "3 triggers" and its `custom_details` carries the
most recent subject line.

## Instances

An instance is a configured, named occurrence of a plugin. Two IMAP readers
watching different mailboxes are two instances of one plugin.

```json
{
  "instances": {
    "Porch Mail": {
      "plugin": "imap-input",
      "config": {
        "host": "mail.example.net",
        "username": "user@example.net",
        "password": "${secret:porch_mail_password}",
        "folder": "Packages",
        "senders": ["amazon.com", "ups.com", "fedex.com", "usps.com"],
        "subject_patterns": ["^Delivered:"],
        "dry_run": true
      }
    }
  }
}
```

| Field     | Required | Meaning                                                              |
| --------- | -------- | -------------------------------------------------------------------- |
| `plugin`  | yes      | The plugin's own identifier, not a name you choose.                   |
| `config`  | no       | Plugin-specific. Each plugin validates its own and reports problems.  |
| `enabled` | no       | Defaults to `true`. A disabled instance drops out of every rule.      |

An unknown `plugin` value is fatal, and the error lists the plugins that did
load — which is how you find out that a plugin failed to import rather than
that you misspelled its name.

## Daemon settings

Optional, and at most one file may contain a `daemon` block. Merging two of
them would reintroduce exactly the silent shadowing that names forbid.

```json
{
  "daemon": {
    "event_log_max_rows": 10000,
    "dispatcher_threads": 4,
    "retry_initial_seconds": 5,
    "retry_max_seconds": 900
  }
}
```

| Field                   | Default | Meaning                                            |
| ----------------------- | ------- | -------------------------------------------------- |
| `event_log_max_rows`    | 10000   | Rolling event log size; oldest rows dropped first.  |
| `dispatcher_threads`    | 4       | Threads performing outbound pushes.                 |
| `retry_initial_seconds` | 5       | First retry delay for a failed push, then doubling. |
| `retry_max_seconds`     | 900     | Ceiling on the retry delay.                         |

## Secrets

Secrets live in `/etc/noti-mapper/secrets.json`, deliberately outside the `.d`
glob so they are not swept up by configuration merging and can carry different
permissions.

```json
{
  "porch_mail_password": "hunter2",
  "pagerduty_routing_key": "R0UT1NGK3Y"
}
```

Reference one from any string inside an instance's `config` block:

```json
{ "password": "${secret:porch_mail_password}" }
```

Substitution works inside a larger string, so
`"imaps://${secret:user}:${secret:password}@host"` is fine.

Rules:

- The daemon **refuses to start** if the file is readable by other users or
  writable by its group, and tells you the offending mode. Two arrangements
  work, and the first is better:

  ```
  install -m 0640 -o root -g noti-mapper secrets.json /etc/noti-mapper/
  install -m 0600 -o noti-mapper -g noti-mapper secrets.json /etc/noti-mapper/
  ```

  Root owning the file means the daemon reads its credentials but cannot
  rewrite them, and the daemon is the part of this system that talks to the
  network. Note that only the *mode* can be checked from here: `0640` with a
  group half the machine belongs to would pass, so pick the group deliberately.

- The daemon must also be able to **search every directory above the file**.
  This is why `/etc/noti-mapper` is `0755` and not `0750` — the service user is
  in no group but its own, so a `root:root 0750` directory locks it out no
  matter how the file inside is owned. If you tighten that directory, give it
  to `root:noti-mapper`.
- A referenced secret that does not exist is a configuration validation error,
  caught by `noti-mapper validate`, not a surprise at 3am.
- Secret names may contain `A-Z a-z 0-9 _ . -`.

The point of the separate file: **configuration files never contain secret
material, so they stay safe to paste into a GitHub issue.** If you are about to
report a bug, `/etc/noti-mapper.d/*.json` can go in verbatim.

## Merging and loading

`/etc/noti-mapper.d/*.json` is read in lexical order. Conventionally:

```
/etc/noti-mapper.d/10-instances.json
/etc/noti-mapper.d/20-rules.json
```

A later file may add instances and rules. Redefining a name an earlier file
already defined is an **error**, not an override. Silent shadowing across
drop-in files is miserable to debug and there is no use case for it here.

The JSON is parsed strictly: duplicate keys within one object are an error,
trailing commas and comments are rejected, and so are `NaN` and `Infinity`.

## What makes the daemon refuse to start

- an unknown plugin
- a duplicate name, compared case-insensitively
- a rule referencing an instance that does not exist
- a rule listing an output instance in `inputs`, or an input in `outputs`
- a missing or invalid plugin setting
- a group- or world-readable secrets file
- malformed JSON

All of them are reported together, each with its file and path.

## Reload

`systemctl reload noti-mapper`, or `SIGHUP`, re-reads the configuration.

- Instances whose plugin and settings are unchanged keep running untouched.
  Reconfiguring one instance does not disturb another's connection.
- Adding, removing, or reconfiguring instances and rules does not drop
  unrelated latch state.
- An invalid configuration on reload is logged and the running configuration is
  kept. The daemon does not fall over because you typed a comma wrong.

### Removing a rule orphans its latch

A rule that configuration no longer defines is marked orphaned. Its latch
record persists and is re-adoptable if a rule with that name comes back, but it
stops contributing to output state immediately — so outputs recompute and may
drop on reload.

`noti-mapper status` lists orphans. `noti-mapper purge` deletes them, along
with the stored plugin state of instances that are also gone. Both are
deliberate acts; nothing is deleted behind your back.

## Plugin settings

### `imap-input`

| Field                       | Required | Default | Notes                                                |
| --------------------------- | -------- | ------- | ---------------------------------------------------- |
| `host`                      | yes      |         |                                                      |
| `username`                  | yes      |         |                                                      |
| `password`                  | yes      |         | Use `${secret:...}`.                                 |
| `senders`                   | no       | any     | Allowlist. Bare domains match subdomains.            |
| `subject_patterns`          | no       | any     | Regular expressions, matched against decoded subject.|
| `port`                      | no       | 993/143 | Follows `ssl`.                                       |
| `ssl`                       | no       | `true`  |                                                      |
| `folder`                    | no       | `INBOX` |                                                      |
| `idle_refresh_seconds`      | no       | 1500    | 25 minutes; small hosts drop before the RFC's 29.    |
| `poll_seconds`              | no       | 60      | Used when the server does not advertise IDLE.        |
| `dry_run`                   | no       | `false` | Evaluate and log; emit nothing.                      |
| `reconnect_initial_seconds` | no       | 5       |                                                      |
| `reconnect_max_seconds`     | no       | 300     |                                                      |
| `catch_up_limit`            | no       | 200     | Most messages examined after downtime.               |

The sender allowlist **and** a subject pattern must both match. A carrier sends
far more mail than delivery notifications.

Both are optional, and leaving one out means "any". A mailbox that a
server-side filter already feeds only delivery mail needs no allowlist; an
address used for nothing else needs no subject patterns:

```json
{
  "host": "mail.example.net",
  "username": "packages@example.net",
  "password": "${secret:packages_password}",
  "folder": "Deliveries"
}
```

That config matches **every message** in `Deliveries`, which is the point when
a filter is doing the work upstream. The reader warns about it at startup
anyway, because a config file that has lost its `subject_patterns` line looks
exactly the same from in here — the warning reads *"Porch Mail has neither a
sender allowlist nor subject patterns, so every message in Deliveries will be
treated as an event."* Leaving out only one of the two is an `INFO` line
saying which.

Writing `"senders": []` is an error rather than a synonym for omitting it. An
empty array is what an edit that removed the last entry leaves behind, and the
cost of reading that as "match everything" is a mailbox that latches on all
mail:

```
noti-mapper: 1 configuration error:
  /etc/noti-mapper.d/10-instances.json: instances → 'Porch Mail' → config:
      instance 'Porch Mail': "senders" is empty; omit it entirely to match any value
```

**Use `dry_run` first.** Set it to `true`, leave it a week, and read the
journal. It will tell you what it would have fired on, which is how you find
out that your carrier also sends "Out for delivery" twice a day. That is the
difference between a setup you keep and one you turn off in irritation.

The mailbox is opened read-only. `\Seen` is never set, nothing is moved,
nothing is deleted, and your mail client's behaviour is completely unaffected.
Message bodies are never parsed.

If `UIDVALIDITY` changes — the server rebuilt the mailbox — every stored UID
becomes meaningless. The reader logs that loudly and re-baselines at the
current highest UID without emitting, so a rebuild does not produce a storm of
false events. Anything that arrived during the rebuild is not seen.

### `webhook-input`

| Field            | Required | Default                  | Notes                              |
| ---------------- | -------- | ------------------------ | ---------------------------------- |
| `secret`         | yes      |                          | At least 16 characters.            |
| `bind`           | no       | `127.0.0.1`              |                                    |
| `port`           | no       | 9736                     | 0 picks a free port.               |
| `path`           | no       | `/`                      | Must start with `/`.               |
| `header`         | no       | `X-Noti-Mapper-Token`    |                                    |
| `max_body_bytes` | no       | 65536                    |                                    |

There is no anonymous mode. A `POST` to the configured path with the correct
header emits an event; the body becomes event metadata, and a JSON object body
is also flattened one level into `json.<key>` entries.

The default bind is localhost. **Exposing this endpoint to the internet is your
decision and your responsibility.** The daemon warns when the bind address is
not loopback, and that is all it can usefully do.

There is no catch-up: a webhook that fired while the daemon was down got a
connection refused and left nothing behind. Senders that need delivery
guarantees should retry.

### `file-input`

| Field              | Required | Default | Notes                                  |
| ------------------ | -------- | ------- | -------------------------------------- |
| `path`             | yes      |         | Absolute. A file, or a directory.      |
| `glob`             | no       |         | Filters a directory; omit for one file.|
| `poll_seconds`     | no       | 1.0     |                                        |
| `debounce_seconds` | no       | 2.0     |                                        |
| `emit_on_modify`   | no       | `true`  | `false` fires only on first appearance.|

Debounce is not optional in practice: editors write a file three times and
rsync writes it in pieces. The event's timestamp is the file's mtime, not the
time the daemon noticed, which is what makes downtime catch-up correct.

### `homekit-output`

| Field          | Required | Default          | Notes                            |
| -------------- | -------- | ---------------- | -------------------------------- |
| `display_name` | no       | the instance name| What you see in the Home app.    |
| `port`         | no       | 51826            | One per instance; give each its own. |
| `address`      | no       |                  | Bind address.                    |
| `manufacturer` | no       | `noti-mapper`    |                                  |
| `model`        | no       | `Latch Switch`   |                                  |

This adds **one accessory to your existing Apple Home setup**. It does not
require migrating your home, bridging devices you already own, or re-pairing
anything.

Turning the switch off is an unlatch. Turning it on is accepted and then
corrected back — only an input can set a latch — which is deliberate, so that
you can confirm the accessory is reachable without being able to fake a
notification.

HAP needs mDNS/Bonjour reachability between the daemon and your Apple Home hub:
the same L2 segment, or an mDNS reflector if your IoT devices live on their own
VLAN.

#### Finding the setup code

The code persists, so a restart before you have finished pairing does not
change it. It is kept in three places, because the journal alone rotates and
is easy to lose:

1. **`noti-mapper status`**, any time, until you pair:

   ```
   Plugin health
     'Desk Lamp'  degraded  (12s ago)  accessory 'Package Waiting' on port 51826; not paired -- setup code 518-08-582
   ```

   An unpaired accessory reports `degraded` rather than `ok` on purpose. It is
   running, but it cannot deliver anything, and this is the line you will be
   reading when you wonder why nothing happens.

2. **`/var/lib/noti-mapper/plugins/<instance>/setup-code.txt`**, mode `0600`,
   rewritten on every start. It holds the code and an `X-HM://` setup URI you
   can render as a QR code and scan with the Home app. It stays after pairing,
   saying so, because removing the accessory and adding it back needs the same
   code.

3. **The journal**, logged on every start until something pairs — not only the
   first start.

Treat the code like a password. Anyone who can reach the accessory on your
network and knows it can pair with it.

### `pagerduty-output`

| Field          | Required | Default                          | Notes                          |
| -------------- | -------- | -------------------------------- | ------------------------------ |
| `routing_key`  | yes      |                                  | **Events API v2 integration key.** |
| `api_token`    | yes      |                                  | **REST API token.**            |
| `dedup_key`    | no       | `noti-mapper/<instance>`         | Stable per instance.           |
| `severity`     | no       | `warning`                        | critical, error, warning, info.|
| `summary`      | no       | `noti-mapper: <instance>`        | Alert title prefix.            |
| `source`       | no       | the instance name                |                                |
| `component`, `group`, `class` | no |                    | Passed through to the payload. |
| `poll_seconds` | no       | 45                               | At least 5.                    |

**The two credentials are different things and are not interchangeable.**
`routing_key` comes from the service's Integrations tab and is what writes
incidents. `api_token` comes from the API Access page and is what reads
incident status, which is how resolving an incident gets back here as an
unlatch. Configuring one and not the other is the predictable first-run
stumble, so the validation message names which is which.

The unlatch path is polling, not an inbound webhook. A webhook would need a
publicly reachable endpoint — tunnel, TLS, attack surface — for a daemon that
usually runs on a home LAN. Polling is outbound-only and resumes cleanly after
an ISP outage.

## Where things live

| Path                                 | What                                    |
| ------------------------------------ | --------------------------------------- |
| `/etc/noti-mapper.d/*.json`          | Configuration.                          |
| `/etc/noti-mapper/secrets.json`      | Secrets, mode 0640 `root:noti-mapper`.  |
| `/etc/noti-mapper/plugins/`          | Locally-authored plugins.               |
| `/usr/lib/noti-mapper/plugins/`      | Packaged plugins.                       |
| `/var/lib/noti-mapper/state.db`      | Latches, retries, plugin scratch, log.  |
| `/usr/share/doc/noti-mapper/examples/`| Example configuration. Not live.       |
