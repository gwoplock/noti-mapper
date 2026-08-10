"""The setup code, and the places it is kept so it can be found again.

Separate from the plugin so that the encoding and the file format can be
tested without constructing an accessory or standing up a driver.

The setup URI is encoded here rather than by calling HAP-python's
``Accessory.xhm_uri()``. That method is only importable when the ``base36``
and ``pyqrcode`` extras are installed -- ``pyhap.accessory`` guards the import
behind ``SUPPORT_QR_CODE`` and the method raises ``NameError`` otherwise --
and those extras are not among the packaged dependencies, so on an ordinary
install the call would fail exactly where the URI is most useful. The payload
below is the HAP setup payload, and the twenty lines that build it are a
better trade than two more packages for one string.
"""

import datetime
import os
from pathlib import Path

# HomeKit accessory category. 8 is Switch, which is what this plugin exposes.
CATEGORY_SWITCH_CODE = 8

# The flags nibble. 2 means "pair over IP", as opposed to BLE or NFC.
SETUP_FLAG_IP = 2

BASE36_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# The encoded payload is always padded out to nine base 36 digits, and the
# setup id is four characters, both fixed by the specification.
ENCODED_PAYLOAD_LENGTH = 9

# The setup code lets anyone on the network pair with the accessory, so the
# file holding it is readable only by the user the daemon runs as.
SETUP_CODE_FILE_MODE = 0o600


def base36_encode(value: int) -> str:
    """Encode a non-negative integer in base 36, most significant digit first."""
    if value == 0:
        return BASE36_DIGITS[0]

    digits: list[str] = []
    while value > 0:
        value, remainder = divmod(value, 36)
        digits.append(BASE36_DIGITS[remainder])
    return "".join(reversed(digits))


def setup_uri(*, setup_code: str, setup_id: str, category: int = CATEGORY_SWITCH_CODE) -> str:
    """Build the ``X-HM://`` setup URI, which a phone camera reads as a QR code.

    The payload is a bit field: three bits of version, four reserved, eight of
    accessory category, four of flags, then the setup code as a 27-bit number
    with its dashes removed.
    """
    digits = setup_code.replace("-", "")

    payload = 0
    payload |= 0 & 0x7  # version
    payload <<= 4
    payload |= 0 & 0xF  # reserved
    payload <<= 8
    payload |= category & 0xFF
    payload <<= 4
    payload |= SETUP_FLAG_IP & 0xF
    payload <<= 27
    payload |= int(digits, 10) & 0x7FFFFFFF

    encoded = base36_encode(payload).rjust(ENCODED_PAYLOAD_LENGTH, "0")
    return f"X-HM://{encoded}{setup_id}"


def setup_code_text(
    *,
    instance_name: str,
    display_name: str,
    setup_code: str,
    uri: str,
    paired: bool,
    written_at: datetime.datetime,
) -> str:
    """The contents of the setup code file.

    Written for someone who has found the file months later with no memory of
    what it is, so it says what the accessory is called, what the code is for,
    and whether it is still needed.
    """
    if paired:
        standing = (
            f"Paired as of {written_at.isoformat()}. You need this code again only if "
            "you remove the accessory from the Home app and add it back."
        )
    else:
        standing = (
            f"Not paired as of {written_at.isoformat()}. Add the accessory in the Home "
            "app -- Add Accessory, then More options -- or scan the setup URI above as "
            "a QR code. This adds one accessory to your existing home; nothing you "
            "already own is affected."
        )

    return (
        f"noti-mapper HomeKit setup code for instance {instance_name!r}\n"
        f"accessory name: {display_name}\n"
        "\n"
        f"setup code: {setup_code}\n"
        f"setup URI:  {uri}\n"
        "\n"
        f"{standing}\n"
        "\n"
        "Anyone who can reach this accessory on the network and knows this code\n"
        "can pair with it. This file is rewritten every time the daemon starts.\n"
    )


def write_setup_code_file(path: Path, text: str) -> None:
    """Write the setup code file, readable only by its owner.

    The mode is passed to ``open`` rather than applied afterwards, so the
    contents are never briefly world-readable. It is then reasserted, because
    ``O_CREAT`` ignores the mode for a file that already exists, and this file
    usually does.

    Only ever this one path, under the plugin's own per-instance directory.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SETUP_CODE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), SETUP_CODE_FILE_MODE)
        handle.write(text)
