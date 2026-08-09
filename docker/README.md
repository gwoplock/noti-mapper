# Running noti-mapper in a container

Two things live here: an image that installs the daemon the way the AUR package
does, and a compose file that the behaviour suite drives.

```bash
# Build it
docker compose -f docker/docker-compose.yml build

# Run the behaviour suite against the container instead of a subprocess
NOTI_BDD_BACKEND=docker behave
```

## What the image is for

It installs into the real paths — `/usr/bin`, `/usr/lib/noti-mapper/plugins`,
`/etc/noti-mapper.d`, `/var/lib/noti-mapper` — creates the service user from the
same `sysusers.d` fragment the package ships, and runs unprivileged. That means
a path assumption the unit tests cannot catch, because they pass every
directory in as an argument, fails here instead of on someone's machine.

It builds the AUR dependency chain rather than pip-installing, so
`python-hap-python` and `python-imapclient` are exercised as packages.

## What it is not for

HomeKit does not work in it. HAP needs mDNS on the same L2 segment as the Apple
Home hub, and a bridged container is not that. The behaviour suite uses the
webhook and file inputs and the PagerDuty output, all of which are observable
from outside the container; HomeKit is covered by unit tests instead.

systemd is not running either, so `Type=notify`, the watchdog, and
`StateDirectory=` are not exercised here. `$STATE_DIRECTORY` is set explicitly
in the image to stand in for the last of those.

## Caveat

**This image has never been built.** It was written on a machine with no
container runtime, so the layer ordering, the AUR build steps, and the
`site-packages` paths copied into the runtime stage are reasoned about rather
than observed. Expect the first `docker compose build` to need corrections,
most likely in the Python version in the `COPY --from=build` paths and in the
`python-imapclient` fetch.
