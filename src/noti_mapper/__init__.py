"""noti-mapper: latch notification events onto outputs until acknowledged.

The importable package is ``noti_mapper``; the distribution, binary, systemd
service, and configuration paths are all spelled ``noti-mapper``.
"""

# The one place the version is written. pyproject reads it from here, and
# scripts/stamp_version.py copies it into the PKGBUILD and the man pages, which
# are not Python and cannot read it themselves.
VERSION: str = "0.0.2"
