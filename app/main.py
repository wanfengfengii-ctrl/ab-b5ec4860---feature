"""Cold-chain exposure HTTP API.

POST /api/cold-chain/exposure

The request body is parsed with ``parse_float=Decimal`` / ``parse_int=Decimal``
so numeric values keep their exact decimal meaning, and timestamps are parsed
to exact Fraction epoch seconds.  As a result neither the order of the
readings nor the decimal representation (``8.1`` vs ``8.10`` vs ``8.100``,
``...:00Z`` vs ``...:00.000+00:00``) can change the conclusion.

Validation failures produce HTTP 422 with FastAPI-style locatable errors::

    {"detail": [{"loc": ["body", "readings", 3, "time"], "msg": "...", "type": "..."}]}
"""
from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .core import compute_exposure
from .timeutil import fraction_to_decimal_str, fraction_to_rfc3339, parse_rfc3339

app = FastAPI(title="Cold-Chain Exposure Service", version="1.0.0")

MIN_READINGS = 2
MAX_READINGS = 500
MIN_THRESHOLD_PERIODS = 1
MAX_THRESHOLD_PERIODS = 16
MAX_ERRORS = 50


# --------------------------------------------------------------------------
# 422 helpers
# --------------------------------------------------------------------------
def _err(loc: list, msg: str, typ: str = "value_error") -> dict:
    return {"loc": ["body", *loc], "msg": msg, "type": typ}


class Unprocessable(Exception):
    def __init__(self, errors: list):
        super().__init__("unprocessable request")
        self.errors = errors


@app.exception_handler(Unprocessable)
async def _unprocessable_handler(_: Request, exc: Unprocessable) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.errors})


def _parse_constant(token: str) -> float:
    # json calls this for NaN / Infinity / -Infinity.  Keep them as floats so
    # the validators can reject them with a precise location.
    return float(token)


# --------------------------------------------------------------------------
# Field validators (append locatable errors, return exact values or None)
# --------------------------------------------------------------------------
def _as_finite_number(value: Any, loc: list, errors: list):
    if isinstance(value, bool):
        errors.append(_err(loc, "expected a number, got a boolean", "type_error.number"))
        return None
    if isinstance(value, float):
        # Only reachable via NaN / Infinity / -Infinity constants.
        errors.append(
            _err(loc, "number must be finite; NaN and Infinity are not allowed",
                 "value_error.number.not_finite")
        )
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            errors.append(
                _err(loc, "number must be finite", "value_error.number.not_finite")
            )
            return None
        return Fraction(value)
    if isinstance(value, int):
        return Fraction(value)
    errors.append(
        _err(loc, f"expected a number, got {type(value).__name__}", "type_error.number")
    )
    return None


def _as_timestamp(value: Any, loc: list, errors: list):
    if not isinstance(value, str):
        errors.append(
            _err(loc, "expected an RFC3339 timestamp string", "type_error.str")
        )
        return None
    try:
        return parse_rfc3339(value)
    except ValueError as exc:
        errors.append(_err(loc, f"invalid RFC3339 timestamp: {exc}", "value_error.datetime"))
        return None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
def root() -> dict:
    return {
        "service": "cold-chain-exposure",
        "version": "1.0.0",
        "endpoint": "POST /api/cold-chain/exposure",
    }


@app.post("/api/cold-chain/exposure")
async def cold_chain_exposure(request: Request) -> JSONResponse:
    raw = await request.body()
    try:
        data = json.loads(
            raw.decode("utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_parse_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Unprocessable([_err([], f"body is not valid JSON: {exc}", "value_error.jsondecode")])

    if not isinstance(data, dict):
        raise Unprocessable([_err([], "expected a JSON object", "type_error.object")])

    errors: list = []

    required = [
        "transport_start",
        "transport_end",
        "threshold_celsius",
        "max_interval_seconds",
        "max_single_excursion_seconds",
        "degree_minute_budget",
        "readings",
    ]
    for key in required:
        if key not in data:
            errors.append(_err([key], "field required", "value_error.missing"))
    if errors:
        raise Unprocessable(errors)

    # ---- scalar fields ----------------------------------------------------
    start = _as_timestamp(data["transport_start"], ["transport_start"], errors)
    end = _as_timestamp(data["transport_end"], ["transport_end"], errors)
    threshold = _as_finite_number(data["threshold_celsius"], ["threshold_celsius"], errors)
    max_interval = _as_finite_number(
        data["max_interval_seconds"], ["max_interval_seconds"], errors
    )
    max_single = _as_finite_number(
        data["max_single_excursion_seconds"], ["max_single_excursion_seconds"], errors
    )
    budget = _as_finite_number(
        data["degree_minute_budget"], ["degree_minute_budget"], errors
    )

    if max_interval is not None and max_interval <= 0:
        errors.append(
            _err(["max_interval_seconds"], "must be greater than 0", "value_error.number.gt")
        )
    if max_single is not None and max_single < 0:
        errors.append(
            _err(["max_single_excursion_seconds"], "must be >= 0", "value_error.number.ge")
        )
    if budget is not None and budget < 0:
        errors.append(
            _err(["degree_minute_budget"], "must be >= 0", "value_error.number.ge")
        )
    if start is not None and end is not None and start >= end:
        errors.append(
            _err(["transport_end"], "must be later than transport_start",
                 "value_error.datetime.ordering")
        )

    # ---- readings ---------------------------------------------------------
    parsed: list = []  # (input_index, Fraction time, Fraction celsius)
    readings = data["readings"]
    if not isinstance(readings, list):
        errors.append(
            _err(["readings"], "expected an array of readings", "type_error.list")
        )
    elif not (MIN_READINGS <= len(readings) <= MAX_READINGS):
        errors.append(
            _err(
                ["readings"],
                f"must contain between {MIN_READINGS} and {MAX_READINGS} readings, "
                f"got {len(readings)}",
                "value_error.list.length",
            )
        )
    else:
        for i, item in enumerate(readings):
            if len(errors) >= MAX_ERRORS:
                errors.append(_err(["readings"], "too many errors; aborting validation"))
                break
            if not isinstance(item, dict):
                errors.append(
                    _err(["readings", i], "expected an object with 'time' and 'celsius'",
                         "type_error.object")
                )
                continue
            t = v = None
            if "time" not in item:
                errors.append(_err(["readings", i, "time"], "field required", "value_error.missing"))
            else:
                t = _as_timestamp(item["time"], ["readings", i, "time"], errors)
            if "celsius" not in item:
                errors.append(_err(["readings", i, "celsius"], "field required", "value_error.missing"))
            else:
                v = _as_finite_number(item["celsius"], ["readings", i, "celsius"], errors)
            if t is not None and v is not None:
                parsed.append((i, t, v))

    if errors:
        raise Unprocessable(errors)

    # ---- semantic checks (duplicates, transport boundaries) ---------------
    seen: dict = {}
    for i, t, _ in parsed:
        if t in seen:
            errors.append(
                _err(
                    ["readings", i, "time"],
                    f"duplicate timestamp: same absolute instant as readings[{seen[t]}]",
                    "value_error.duplicate",
                )
            )
        else:
            seen[t] = i
    if errors:
        raise Unprocessable(errors)

    points = sorted((t, v) for _, t, v in parsed)  # sort by absolute time

    if points[0][0] != start:
        idx = seen[points[0][0]]
        errors.append(
            _err(
                ["readings", idx, "time"],
                "earliest reading must be exactly at transport_start "
                f"({fraction_to_rfc3339(start)})",
                "value_error.boundary",
            )
        )
    if points[-1][0] != end:
        idx = seen[points[-1][0]]
        errors.append(
            _err(
                ["readings", idx, "time"],
                "latest reading must be exactly at transport_end "
                f"({fraction_to_rfc3339(end)})",
                "value_error.boundary",
            )
        )
    if errors:
        raise Unprocessable(errors)

    # ---- threshold_periods (optional, ordered-invariant) ------------------
    # Each item: {"start": RFC3339, "end": RFC3339, "threshold_celsius": num}.
    # When present the 1..16 periods must be adjacent, non-overlapping and
    # cover [transport_start, transport_end] exactly.  At a shared boundary
    # the NEW period's threshold is in effect.  Input order is irrelevant:
    # periods are sorted before use, and every cross-period error is reported
    # at the offending entry's ORIGINAL index.
    threshold_periods = None
    periods_raw = data.get("threshold_periods")
    if periods_raw is not None:
        if not isinstance(periods_raw, list):
            errors.append(
                _err(["threshold_periods"], "expected an array of threshold periods",
                     "type_error.list")
            )
        elif not (MIN_THRESHOLD_PERIODS <= len(periods_raw) <= MAX_THRESHOLD_PERIODS):
            errors.append(
                _err(
                    ["threshold_periods"],
                    f"must contain between {MIN_THRESHOLD_PERIODS} and "
                    f"{MAX_THRESHOLD_PERIODS} periods, got {len(periods_raw)}",
                    "value_error.list.length",
                )
            )
        else:
            parsed_periods: list = []  # (input_index, start, end, threshold)
            for i, item in enumerate(periods_raw):
                loc = ["threshold_periods", i]
                if not isinstance(item, dict):
                    errors.append(
                        _err(loc, "expected an object with 'start', 'end' and "
                             "'threshold_celsius'", "type_error.object")
                    )
                    continue
                for key in ("start", "end", "threshold_celsius"):
                    if key not in item:
                        errors.append(_err([*loc, key], "field required",
                                           "value_error.missing"))
                pt = pe = pv = None
                if "start" in item:
                    pt = _as_timestamp(item["start"], [*loc, "start"], errors)
                if "end" in item:
                    pe = _as_timestamp(item["end"], [*loc, "end"], errors)
                if "threshold_celsius" in item:
                    pv = _as_finite_number(
                        item["threshold_celsius"], [*loc, "threshold_celsius"], errors
                    )
                if pt is not None and pe is not None and pv is not None:
                    if pt >= pe:
                        errors.append(
                            _err([*loc, "end"],
                                 "period end must be later than period start",
                                 "value_error.datetime.ordering")
                        )
                    else:
                        parsed_periods.append((i, pt, pe, pv))

        if not errors and parsed_periods:
            ordered = sorted(parsed_periods, key=lambda x: x[1])
            idx0, p0_start, _, _ = ordered[0]
            idxN, _, pN_end, _ = ordered[-1]
            if p0_start != start:
                errors.append(
                    _err(
                        ["threshold_periods", idx0, "start"],
                        "first period must start at transport_start "
                        f"({fraction_to_rfc3339(start)})",
                        "value_error.boundary",
                    )
                )
            if pN_end != end:
                errors.append(
                    _err(
                        ["threshold_periods", idxN, "end"],
                        "last period must end at transport_end "
                        f"({fraction_to_rfc3339(end)})",
                        "value_error.boundary",
                    )
                )
            for (ia, _, ea, _va), (ib, sb, _, _vb) in zip(ordered, ordered[1:]):
                if ea > sb:
                    errors.append(
                        _err(
                            ["threshold_periods", ib, "start"],
                            f"period[{ib}] overlaps period[{ia}]: its start "
                            f"{fraction_to_rfc3339(sb)} precedes the previous "
                            f"period end {fraction_to_rfc3339(ea)}",
                            "value_error.overlap",
                        )
                    )
                elif ea < sb:
                    errors.append(
                        _err(
                            ["threshold_periods", ib, "start"],
                            f"coverage hole between periods: period[{ia}] ends at "
                            f"{fraction_to_rfc3339(ea)} but period[{ib}] starts at "
                            f"{fraction_to_rfc3339(sb)}; periods must be adjacent "
                            "and cover the whole transport",
                            "value_error.coverage",
                        )
                    )
            if not errors:
                threshold_periods = [(s, e, v) for _, s, e, v in ordered]

        if errors:
            raise Unprocessable(errors)

    # ---- adjudication -----------------------------------------------------
    result = compute_exposure(points, threshold, max_interval, threshold_periods)

    reasons: list = []
    for i, gap in enumerate(result.gaps):
        reasons.append(
            {
                "code": "coverage_gap",
                "gap_index": i,
                "message": (
                    f"coverage gap from {fraction_to_rfc3339(gap.start)} to "
                    f"{fraction_to_rfc3339(gap.end)} "
                    f"({fraction_to_decimal_str(gap.duration_seconds)}s) exceeds "
                    f"max_interval_seconds {fraction_to_decimal_str(max_interval)}"
                ),
            }
        )
    for i, exc in enumerate(result.excursions):
        if exc.duration_seconds > max_single:
            reasons.append(
                {
                    "code": "single_excursion_exceeded",
                    "excursion_index": i,
                    "duration_seconds": fraction_to_decimal_str(exc.duration_seconds),
                    "limit_seconds": fraction_to_decimal_str(max_single),
                    "message": (
                        f"excursion {i} lasts "
                        f"{fraction_to_decimal_str(exc.duration_seconds)}s, exceeding "
                        f"max_single_excursion_seconds "
                        f"{fraction_to_decimal_str(max_single)}"
                    ),
                }
            )
    if result.total_degree_minutes > budget:
        reasons.append(
            {
                "code": "degree_minute_budget_exceeded",
                "total_degree_minutes": fraction_to_decimal_str(result.total_degree_minutes),
                "budget_degree_minutes": fraction_to_decimal_str(budget),
                "message": (
                    f"total degree-minutes "
                    f"{fraction_to_decimal_str(result.total_degree_minutes)} exceeds "
                    f"degree_minute_budget {fraction_to_decimal_str(budget)}"
                ),
            }
        )

    return JSONResponse(
        {
            "verdict": "fail" if reasons else "pass",
            "reasons": reasons,
            "excursions": [
                {
                    "start": fraction_to_rfc3339(exc.start),
                    "end": fraction_to_rfc3339(exc.end),
                    "duration_seconds": fraction_to_decimal_str(exc.duration_seconds),
                    "degree_minutes": fraction_to_decimal_str(exc.degree_minutes),
                }
                for exc in result.excursions
            ],
            "total_degree_minutes": fraction_to_decimal_str(result.total_degree_minutes),
            "coverage_gaps": [
                {
                    "start": fraction_to_rfc3339(gap.start),
                    "end": fraction_to_rfc3339(gap.end),
                    "duration_seconds": fraction_to_decimal_str(gap.duration_seconds),
                }
                for gap in result.gaps
            ],
        }
    )
