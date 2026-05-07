"""OCC option symbol encoder/decoder.

Public uses the standard OCC 21-character symbology:

    AAPL  240216  C  00140000
    └──┬──┘└──┬──┘ │ └────┬────┘
    root    YYMMDD  C/P   strike × 1000, zero-padded to 8 digits

Examples:
    AAPL240216C00140000  → AAPL, 2024-02-16, CALL, $140.00
    SPY260508P00580000   → SPY,  2026-05-08, PUT,  $580.00

The root is left-padded with spaces to 6 chars in the formal OCC standard,
but Public (and most modern APIs) omits the padding. We do the same.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


_OCC_RE = re.compile(
    r"^(?P<root>[A-Z\.]{1,6})"
    r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<cp>[CP])"
    r"(?P<strike>\d{8})$"
)


@dataclass(frozen=True)
class OccOption:
    root: str
    expiration: date
    option_type: str  # "CALL" | "PUT"
    strike: float

    @property
    def symbol(self) -> str:
        return encode(self.root, self.expiration, self.option_type, self.strike)


def encode(root: str, expiration: date, option_type: str, strike: float) -> str:
    """Build an OCC option symbol."""
    if option_type not in ("CALL", "PUT"):
        raise ValueError(f"option_type must be CALL or PUT, got {option_type}")
    if strike <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    yy = f"{expiration.year % 100:02d}"
    mm = f"{expiration.month:02d}"
    dd = f"{expiration.day:02d}"
    cp = "C" if option_type == "CALL" else "P"
    strike_int = round(strike * 1000)
    return f"{root.upper()}{yy}{mm}{dd}{cp}{strike_int:08d}"


def decode(symbol: str) -> OccOption:
    """Parse an OCC option symbol."""
    m = _OCC_RE.match(symbol.strip())
    if not m:
        raise ValueError(f"Not a valid OCC option symbol: {symbol!r}")
    yy = int(m.group("yy"))
    # Years 70-99 → 1970-1999, 00-69 → 2000-2069 (industry convention)
    year = 2000 + yy if yy < 70 else 1900 + yy
    return OccOption(
        root=m.group("root"),
        expiration=date(year, int(m.group("mm")), int(m.group("dd"))),
        option_type="CALL" if m.group("cp") == "C" else "PUT",
        strike=int(m.group("strike")) / 1000.0,
    )
