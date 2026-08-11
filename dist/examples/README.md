# Example configuration

These are installed to `/usr/share/doc/noti-mapper/examples/` and are **not**
live. Nothing here is read by the daemon.

A package-installed configuration that goes live on first boot is a bad
surprise: the daemon would start watching a mailbox nobody asked it to watch.
Copy what you want into place instead:

```
install -m 0644 /usr/share/doc/noti-mapper/examples/10-instances.json /etc/noti-mapper.d/
install -m 0644 /usr/share/doc/noti-mapper/examples/20-rules.json     /etc/noti-mapper.d/
install -m 0640 -o root -g noti-mapper \
    /usr/share/doc/noti-mapper/examples/secrets.json /etc/noti-mapper/secrets.json
```

Then edit them, and check your work before restarting:

```
noti-mapper validate
```

Note that `10-instances.json` ships with `"dry_run": true` on the IMAP reader.
Leave it that way for a week. The daemon will connect, evaluate every message,
and log what it *would* have done without emitting anything, which is how you
find out that your carrier also sends "Out for delivery" twice a day.

`secrets.json` lives outside `/etc/noti-mapper.d/` deliberately, so that it is
not swept up by configuration merging and can carry its own permissions. Root
owns it and the daemon's group reads it, which means a compromised daemon
cannot rewrite its own credentials; mode 0600 owned by `noti-mapper` works too.
The daemon refuses to start if the file is readable by other users or writable
by its group. Because secrets never appear in the configuration files
themselves, those files stay safe to paste into a bug report.
