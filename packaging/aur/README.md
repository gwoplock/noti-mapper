# AUR packaging

Two packages, because HAP-python has no Arch package and its own install
documentation assumes a virtualenv and `pip`. Declaring system dependencies
properly costs a second AUR package to maintain; vendoring a venv inside
`noti-mapper` would have been cheaper and wrong.

- `noti-mapper/` — the daemon.
- `python-hap-python/` — the HomeKit library, published separately so
  `noti-mapper` can simply depend on it.

AUR is the only packaging target. No deb, no rpm, no container image, no Nix,
and nothing in the build is structured around the possibility of adding them.

## Dependency notes

`python-imapclient` is already in the AUR, and other AUR packages depend on it,
so an AUR-to-AUR dependency is an established pattern here. Its comment history
shows it has gone stale before. Expect to adopt or fork it if that happens
again; the alternative is hand-rolling an IMAP client, which is the single
largest source of subtle bugs this project could acquire.

`avahi` is declared even though nothing imports it, because HAP-python's
zeroconf layer needs Bonjour present on the host. Its absence produces a
confusing runtime failure rather than an install-time one, which is the whole
argument for naming it as a dependency.

## Before submitting a release

- [ ] Confirm which repository currently provides `python-zeroconf`. It has
      moved between `community` and `extra` over the years; the PKGBUILD does
      not care, but the submission comment should be accurate.
- [ ] Confirm `python-chacha20poly1305-reuseable`, `python-orjson`, and
      `python-h11` are all available, and whether any of them are themselves
      AUR packages. A chain of AUR dependencies is acceptable but should be
      stated up front in the package description rather than discovered by the
      first person who installs it.
- [ ] Replace the `SKIP` checksums with real ones once the release tarball
      exists.
- [ ] Check the HAP-python version against upstream and update `pkgver` and its
      dependency list together. HAP-python's requirements have changed shape
      more than once.
- [ ] Build in a clean chroot (`extra-x86_64-build`) so that a dependency
      satisfied only by the maintainer's machine is caught.
- [ ] `namcap` both PKGBUILDs and both resulting packages.

## What the package does and does not install

Installed:

- `/usr/bin/noti-mapper`
- `/usr/lib/noti-mapper/plugins/` — the shipped plugins, as plain directories,
  because discovery is a filesystem scan
- `/usr/lib/systemd/system/noti-mapper.service`
- `/usr/lib/sysusers.d/noti-mapper.conf`
- man pages, documentation, and example configuration under
  `/usr/share/doc/noti-mapper/`
- empty `/etc/noti-mapper/` and `/etc/noti-mapper/plugins/`

Deliberately not installed:

- anything in `/etc/noti-mapper.d/`. The unit's `ConfigurationDirectory=`
  creates the directory; the package never puts a configuration file in it. A
  package-installed configuration that goes live on first boot would have the
  daemon watching a mailbox nobody asked it to watch.
- `/etc/noti-mapper/secrets.json`. The admin creates it, mode 0600.
- the state directory. `StateDirectory=` in the unit handles it, with the right
  ownership and without a tmpfiles rule.
