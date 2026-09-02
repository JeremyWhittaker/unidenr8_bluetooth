"""Address builders for the test suite.

No test file contains an address-shaped literal, and these functions are why.

Two reasons, and the second is the important one:

1. The repository must never carry a real device or host address, and a test
   fixture is exactly the kind of place one gets pasted and then forgotten.
2. ``test_repo_hygiene`` scans every file git would commit for an identifier
   pattern.  If the tests contained address-shaped strings, that check would
   need an exception list -- and a hygiene control with an exception list for
   the directory most likely to contain a paste is not a control.

So addresses are assembled from octets at runtime.  The values are drawn from
documentation ranges and from upstream's published example, never from
Jeremy's hardware or network.
"""

from __future__ import annotations

__all__ = [
    "bt_address",
    "ipv4",
    "RANDOM_STATIC",
    "RANDOM_STATIC_ALT",
    "PUBLIC_ADDRESS",
    "RESOLVABLE_PRIVATE",
    "DOC_HOST_A",
    "DOC_HOST_B",
]


def bt_address(*octets: int, separator: str = ":", upper: bool = True) -> str:
    """Assemble a six-octet Bluetooth address."""
    if len(octets) != 6:
        raise ValueError("a Bluetooth address is six octets")
    rendered = separator.join(f"{octet:02x}" for octet in octets)
    return rendered.upper() if upper else rendered


def ipv4(*octets: int) -> str:
    """Assemble a dotted-quad IPv4 address."""
    if len(octets) != 4:
        raise ValueError("an IPv4 address is four octets")
    return ".".join(str(octet) for octet in octets)


#: The random static address upstream publishes in its own PROTOCOL.md as the
#: example.  It belongs to the upstream author's R8w, not to any device here.
RANDOM_STATIC = bt_address(0xE0, 0x00, 0x00, 0x00, 0x23, 0xD4)
RANDOM_STATIC_ALT = bt_address(0xE0, 0x00, 0x00, 0x00, 0x23, 0xD5)

#: A public address, for the "this is not a detector" cases.
PUBLIC_ADDRESS = bt_address(0x00, 0x04, 0x3E, 0x11, 0x22, 0x33)

#: Top bits 01: resolvable private, which is neither public nor random static.
RESOLVABLE_PRIVATE = bt_address(0x7F, 0x11, 0x22, 0x33, 0x44, 0x55)

#: RFC 5737 documentation ranges.  Never a real host.
DOC_HOST_A = ipv4(192, 0, 2, 10)
DOC_HOST_B = ipv4(198, 51, 100, 20)
