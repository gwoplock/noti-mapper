# Writing a plugin

A plugin is a directory containing `__init__.py` that exposes three or four
well-known names. That is the whole contract, and you can inspect the set of
installed plugins with `ls`.

There is no entry-point registration, no `pip install`, and nothing that
depends on the Python packaging ecosystem. Drop a directory in
`/etc/noti-mapper/plugins/` and restart.

## Where plugins are found

Scanned in this order:

1. the in-tree `plugins/` directory, when running from a source checkout
2. `/usr/lib/noti-mapper/plugins/` — packaged
3. `/etc/noti-mapper/plugins/` — yours

The first directory providing a given `PLUGIN_NAME` wins, and a later duplicate
is logged as a load failure rather than silently shadowing.

A plugin that fails to import is logged with its traceback and skipped: one
broken third-party plugin must never stop the daemon starting. A configuration
file referencing a plugin that failed to load is still a fatal configuration
error, with a message listing the plugins that did load.

## The well-known names

```python
PLUGIN_NAME = "my-thing"     # what users put in a config file's "plugin" field
INPUT_PLUGIN = MyInput       # optional
OUTPUT_PLUGIN = MyOutput     # optional
```

At least one of `INPUT_PLUGIN` and `OUTPUT_PLUGIN` is required. A plugin may
provide both, in which case an instance is used as whichever direction a rule
names it in.

## A minimal input

```python
import datetime
import threading
from collections.abc import Mapping

from noti_mapper.plugin import (
    EmitCallback,
    InputPlugin,
    ObservedEvent,
    PluginContext,
    PluginHealth,
)
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "heartbeat-input"


class HeartbeatInput(InputPlugin):
    """Emits an event every `interval_seconds`. Useful mostly as an example."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        interval = settings.get("interval_seconds")
        if interval is None:
            return ['"interval_seconds" is required']
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
            return ['"interval_seconds" must be a positive integer']
        return []

    def __init__(self, *, context: PluginContext, emit: EmitCallback) -> None:
        super().__init__(context=context, emit=emit)
        self._interval = int(str(context.settings["interval_seconds"]))
        self._stop = threading.Event()

    def start(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            self.emit(
                ObservedEvent(
                    occurred_at=self.context.clock.now(),
                    metadata={"source": "heartbeat"},
                )
            )

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> PluginHealth:
        return PluginHealth(status=HealthStatus.OK, detail=f"every {self._interval}s")

    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
        return []


INPUT_PLUGIN = HeartbeatInput
```

### What the core guarantees

- `start()` runs on a thread of its own and is expected to block. Blocking IMAP
  IDLE in its own thread is the intended shape; there is no async plumbing to
  fit into.
- `stop()` is called from another thread and must cause `start()` to return.
- `health()` is called from the core thread. **Do not block in it.** Keep a
  status field behind a lock and return it.
- `emit()` is safe to call from your thread. It puts a message on the core
  queue and returns immediately.

### `catch_up` is the interesting one

```python
def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
```

Return events that happened while the daemon was stopped. `since` is the last
time the daemon is known to have been running, or `None` if there is no such
record.

**The timestamp on a returned event must be when the thing actually happened**
— the message date, the file's mtime — and not the time you noticed it.
Reconciliation compares that timestamp against the time an output reports being
cleared, and the whole "cleared remotely while down, then a new event arrived"
case turns on it. Returning `clock.now()` here breaks that silently.

If your source cannot answer the question, return an empty list, or raise. The
core logs the failure, falls back to persisted state rather than blocking
startup, and retries in the background.

If there is genuinely nothing to catch up on — a webhook receiver, for example,
where a sender that fired during downtime got a connection refused and left
nothing behind — return `[]` and say why in a docstring.

## A minimal output

```python
from collections.abc import Mapping

from noti_mapper.plugin import (
    OutputPlugin,
    OutputUpdate,
    PluginContext,
    PluginError,
    PluginHealth,
    RemoteBelief,
    RemoteState,
    UnlatchCallback,
)
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "logfile-output"


class LogFileOutput(OutputPlugin):
    def __init__(self, *, context: PluginContext, request_unlatch: UnlatchCallback) -> None:
        super().__init__(context=context, request_unlatch=request_unlatch)
        self._path = Path(str(context.settings["path"]))

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        if "path" not in settings:
            return ['"path" is required']
        return []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def apply(self, update: OutputUpdate) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(f"{update.state} {update.summary()}\n")
        except OSError as error:
            raise PluginError(f"cannot write {self._path}: {error}") from error

    def query(self) -> RemoteState:
        return RemoteState(belief=RemoteBelief.UNKNOWN)

    def health(self) -> PluginHealth:
        return PluginHealth(status=HealthStatus.OK)


OUTPUT_PLUGIN = LogFileOutput
```

### Outputs are bidirectional

This is unusual for a sink abstraction and it is the crux of this project. An
output both renders latch state *and sources unlatch requests*. Do not model
one as write-only: an output that cannot tell the core "the operator cleared
this" leaves the user with no way to unlatch.

Call `self.request_unlatch("why")` from whatever thread notices — a poller, a
callback from a library, an incoming connection. It is safe from any thread and
returns immediately.

**It is also idempotent by construction.** A request against already-cleared
state is a logged no-op, not a second transition. That means a poller that
observes a clear the daemon itself caused does no harm, and you do not have to
track which clears were yours. This is what stops output A's clear from
bouncing back through output B's poller forever.

### `apply` must not veto state

`apply()` runs on a dispatcher thread, never the core thread, so blocking on
the network is fine. Raise `PluginError` when it fails; the push is retried
with exponential backoff, and the latch is completely unaffected. If PagerDuty
is unreachable the latch still sets and the lamp still turns on.

`apply()` **must be idempotent.** The same value is applied repeatedly — every
startup reconciliation forces a push to every output whether or not the daemon
believes it is already correct.

The retry always applies *current* state. The value is recomputed at dispatch
time, so you will never be handed a stale value that failed several minutes
ago.

### What `OutputUpdate` carries

```python
update.state           # bool: what to render
update.cause           # the instance name that most recently set it
update.detail          # a one-line rendering of the event that did it
update.trigger_count   # total triggers across every rule driving this output
update.rules           # names of the rules currently driving it true
update.since           # when the most recent of them was set
update.summary()       # detail, with "(3 triggers)" appended when relevant
```

A bare boolean would be enough to turn a lamp on. It is not enough to say "3
packages waiting" or to put a subject line in a PagerDuty payload, which is why
the update carries more than the value.

### `query` and reconciliation

```python
def query(self) -> RemoteState:
```

Report what the remote end currently believes, so reconciliation can decide
what a latch should be after downtime.

- `RemoteBelief.ACTIVE` — still outstanding. This *confirms* a set latch but
  never resurrects a cleared one.
- `RemoteBelief.CLEARED` — cleared. **Include `cleared_at` if you possibly
  can.** A clear with no timestamp is treated as "cleared at some unknown time
  in the past" and loses to any input event, which is the safe direction but
  less accurate than the truth.
- `RemoteBelief.UNKNOWN` — you could not reach it, or it has no opinion.
  Reconciliation falls back to persisted state and retries you in the
  background.

Return `UNKNOWN` rather than guessing. In particular, "there is no incident and
there never was one" is `UNKNOWN`, not `CLEARED`: never having alerted is not
the same as having been cleared, and reporting `CLEARED` there would drop a
latch on the strength of nothing.

If your remote genuinely holds no state — the HomeKit accessory is a good
example, since a controller does not remember anything while the accessory is
unreachable — `UNKNOWN` is the correct and honest answer.

## `PluginContext`

Every instance is constructed with one:

```python
context.instance_name    # the name the user typed
context.settings         # the "config" block, with ${secret:...} already resolved
context.storage          # durable per-instance key/value storage
context.clock            # use this, not datetime.now()
context.logger           # a logger namespaced to this instance
context.state_directory  # for libraries that insist on a file path
context.state_path("x")  # a per-instance path under it, parent created
```

### Use `context.storage`, not your own files

```python
self.context.storage.set("last_uid", "4242")
value = self.context.storage.get("last_uid")   # str | None
```

It is backed by the same SQLite file as everything else, so there is one thing
to back up and one thing to migrate. It is scoped to your instance, so two
instances of your plugin cannot collide. Reach for `state_path()` only when a
library demands a filename — HAP-python's pairing state is the case that forced
it to exist.

### Use `context.clock`

`clock.now()` returns timezone-aware UTC and `clock.monotonic()` never goes
backwards. Using them instead of `datetime.now()` and `time.monotonic()` is
what lets a test drive your plugin with a manual clock rather than sleeping.

## `validate_settings`

```python
@classmethod
def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
```

Return **every** problem, not just the first. Configuration validation collects
these across all instances and reports them in one pass, so a user fixes
everything at once instead of one restart at a time.

Reject unknown keys. A typo that is silently ignored is a support request:

```python
for key in settings:
    if key not in REQUIRED_KEYS and key not in OPTIONAL_KEYS:
        problems.append(f'unknown setting "{key}"')
```

Write messages that say what to do. Compare:

> `"api_token" is missing`

with what the PagerDuty output actually says:

> `"api_token" is required. This is a REST API token from PagerDuty's API
> Access page, and it is not the same credential as "routing_key". It is what
> reads incident status, which is how an unlatch gets back here.`

## Multiple files

A plugin directory is a package, so split it up when it grows. Import your own
submodules **relatively**:

```python
from .matching import Criteria
```

Not absolutely. The directory is loaded by path under a synthetic module name,
and no absolute name is simultaneously correct for the loader, a type checker,
and a test suite. Relative imports resolve correctly under all three.

Keep the parts that do not need a third-party library in their own module. The
IMAP reader's matching rules live in `matching.py` with no `imapclient` import,
which is what lets them be tested against a corpus of real carrier subject
lines without a mail server anywhere in sight.

## Third-party libraries

Import them at module level. If the library is missing, the plugin fails to
load, the traceback goes in the journal, and a configuration referencing it
fails validation with a message listing the plugins that did load. That is the
right failure: loud, early, and pointing at the missing package.

Declare the library as a system dependency in the packaging. Do not vendor it.

## Testing your plugin

`noti_mapper.clock.ManualClock` is shipped for you rather than kept in the test
tree, precisely so that a plugin author does not have to reimplement it:

```python
from noti_mapper.clock import ManualClock

clock = ManualClock()
clock.advance(60)
```

Prefer structuring the plugin so a test can step it. Both the file watcher and
the PagerDuty output expose a public `poll_once()`, and the loop in `start()`
is a timer around it and nothing more. That is worth copying: it makes debounce
and polling behaviour testable without sleeping.

## Style

The codebase is written to be read by a C++ engineer who does not write Python
daily. Follow it:

- Type hints on everything; `mypy --strict` is run in CI.
- `@dataclass` for structs, `enum.Enum` for enums, ABCs for interfaces.
- Explicit `threading.Lock` and `queue.Queue`, and threads with names.
- Named arguments at call sites for anything taking more than two parameters.
- Small functions with one job and an explicit `return`.
- No metaclasses, no `__getattr__`, no monkey-patching, no `*args`/`**kwargs`
  passthrough that hides a real signature.

## Checklist

- [ ] `PLUGIN_NAME`, and `INPUT_PLUGIN` and/or `OUTPUT_PLUGIN`
- [ ] `validate_settings` returns every problem and rejects unknown keys
- [ ] `health()` does not block
- [ ] `stop()` causes `start()` to return
- [ ] Inputs: `catch_up` timestamps are when it happened, not when you noticed
- [ ] Outputs: `apply` is idempotent and raises `PluginError` on failure
- [ ] Outputs: `query` returns `UNKNOWN` rather than guessing, and includes
      `cleared_at` when it reports `CLEARED`
- [ ] Outputs: there is a path that calls `request_unlatch`
- [ ] State goes in `context.storage`, time comes from `context.clock`
