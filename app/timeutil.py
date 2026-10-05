"""Exact RFC3339 parsing and exact decimal/RFC3339 formatting.

All timestamps are converted to ``fractions.Fraction`` seconds since the Unix
epoch.  Parsing is exact: two different textual representations of the same
absolute instant (``...:00Z`` vs ``...:00.000Z`` vs an offset form such as
``...+02:00``) map to the *same* Fraction, so conclusions never depend on the
decimal representation chosen by the caller.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction

_EPOCH = date(1970, 1, 1)

_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt]"  # date
    r"(\d{2}):(\d{2}):(\d{2})"  # time
    r"(?:\.(\d+))?"  # optional fractional seconds
    r"([Zz]|[+-]\d{2}:\d{2})$"  # UTC offset
)


def parse_rfc3339(text: str) -> Fraction:
    """Parse an RFC3339 timestamp into exact epoch seconds (Fraction).

    Raises ValueError with a human-readable reason for anything invalid.
    """
    m = _RFC3339_RE.match(text)
    if not m:
        raise ValueError(
            "expected RFC3339 format YYYY-MM-DDThh:mm:ss[.fraction](Z|+hh:mm|-hh:mm)"
        )
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac_digits = m.group(7)
    offset = m.group(8)

    try:
        d = date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid date: {exc}") from exc
    if hour > 23:
        raise ValueError("hour out of range 00-23")
    if minute > 59:
        raise ValueError("minute out of range 00-59")
    if second > 59:
        raise ValueError("second out of range 00-59 (leap seconds are not supported)")

    base = (d - _EPOCH).days * 86400 + hour * 3600 + minute * 60 + second

    if offset in ("Z", "z"):
        offset_seconds = 0
    else:
        sign = 1 if offset[0] == "+" else -1
        off_h, off_m = int(offset[1:3]), int(offset[4:6])
        if off_h > 23 or off_m > 59:
            raise ValueError("invalid UTC offset")
        offset_seconds = sign * (off_h * 3600 + off_m * 60)

    value = Fraction(base - offset_seconds)
    if frac_digits:
        value += Fraction(int(frac_digits), 10 ** len(frac_digits))
    return value


def fraction_to_rfc3339(t: Fraction) -> str:
    """Format epoch seconds back to RFC3339 UTC, rounded to nanoseconds."""
    ns = round(t * 1_000_000_000)  # round() on Fraction: nearest, ties to even
    sec, nano = divmod(ns, 1_000_000_000)
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=sec)
    out = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if nano:
        out += "." + f"{nano:09d}".rstrip("0")
    return out + "Z"


def fraction_to_decimal_str(x: Fraction, places: int = 9) -> str:
    """Format a Fraction as a plain decimal string.

    Terminating values are rendered exactly; non-terminating values are
    rounded half-up to ``places`` decimal places (display only — all verdict
    arithmetic is done on the exact Fractions).
    """
    with localcontext() as ctx:
        ctx.prec = 100
        dec = Decimal(x.numerator) / Decimal(x.denominator)
    quantum = Decimal(1).scaleb(-places)
    rounded = dec.quantize(quantum, rounding=ROUND_HALF_UP)
    s = format(rounded.normalize(), "f")
    return "0" if s == "-0" else s
