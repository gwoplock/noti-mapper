# Plugins

One directory per plugin. Each directory is a Python module (it has an
`__init__.py`) exposing well-known attributes:

- `PLUGIN_NAME` — the identifier users put in a config file's `plugin` field.
- `INPUT_PLUGIN` and/or `OUTPUT_PLUGIN` — class references.

Discovery is a filesystem scan, in this order:

1. this in-tree directory, when running from a source checkout
2. `/usr/lib/noti-mapper/plugins/` — packaged plugins
3. `/etc/noti-mapper/plugins/` — locally-authored plugins

There is no entry-point registration, no `pip install`, and no packaging-system
involvement. A plugin is a directory in a known location, and that is the whole
contract — you can inspect it with `ls`.

See `docs/plugin-authoring.md`.
