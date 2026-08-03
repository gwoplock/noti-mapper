# noti-mapper

Latch notification events onto outputs until you explicitly acknowledge them.

An input fires an **event** — a package-delivered mail arrives, a webhook is
POSTed, a file appears. noti-mapper **latches** that event into persistent state
and drives every mapped **output** true. The latch stays set until an output
sends an **unlatch**: you flip the HomeKit switch off, or you resolve the
PagerDuty incident. Then everything mapped to that rule goes quiet together.

That is the whole product. Inputs and outputs are booleans, rules are straight
routing, and the core owns the state.

## Why this exists

"Notify me until I acknowledge" sounds like a one-afternoon script, and the
first 80% is. The remaining 20% is where every homegrown version breaks:

- **Reconciliation after downtime.** The daemon was off. Mail arrived. You also
  resolved the incident from your phone. Which wins? (The later timestamp does —
  and getting that backwards silently drops the event.)
- **Two clear paths racing.** Clearing from the lamp resolves the pager, whose
  next poll sees the resolve and sends its own unlatch. That second unlatch must
  be a no-op, not a second transition.
- **Retrying a failed push without losing state.** PagerDuty being unreachable
  must not veto the latch. The lamp still turns on; the push retries; the retry
  applies *current* state, never the stale value that failed.

noti-mapper factors that out. Plugins do only I/O.

## HomeKit users, read this first

The HomeKit output adds a single Switch accessory to your **existing** Apple Home
setup. It does not require migrating your home, bridging your existing devices,
or re-pairing anything you already own. You add one accessory, and that
accessory is the latch.

It does need mDNS/Bonjour reachability between the daemon and your Apple Home
hub — same L2 segment, or an mDNS reflector if your IoT devices live on their own
VLAN.

## Status

Pre-release. See `docs/` for the configuration reference and the plugin
authoring guide.

## Plugins

Inputs: IMAP reader, webhook receiver, file watcher.
Outputs: HomeKit switch, PagerDuty incident.

Outputs are bidirectional by design — an output both renders latch state and
sources unlatch requests. See `docs/plugin-authoring.md`.

## Non-goals

No non-boolean values. No conditional logic, schedules, or expressions in rules.
No web UI. No inter-plugin communication. No auto-clearing heuristics —
unlatching is always deliberate.

## License

MIT. See [LICENSE.md](LICENSE.md).
