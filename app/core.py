"""Cold-chain thermal exposure engine.

All arithmetic uses ``fractions.Fraction`` so every crossing point, duration
and degree-minute total is exact and independent of how the input decimals
were written.

Rules implemented here:
* Readings are evaluated in absolute-time order (caller sorts first).
* Between two adjacent readings the temperature is assumed to vary linearly,
  but only when the gap between them does not exceed ``max_interval``.
* A wider gap is a *coverage gap*: no interpolation happens across it and it
  breaks any excursion in progress.
* Only temperatures *strictly* above the applicable threshold count as
  exposure; a reading exactly equal to the threshold contributes nothing.
* The threshold may vary by period (``threshold_periods``): adjacent,
  non-overlapping periods covering the whole transport, each with its own
  constant threshold.  A sampling segment is split at every period boundary;
  crossings are solved exactly on each piece (linear trajectory vs. the
  period's constant threshold) and exposure is accumulated against each
  period's own threshold -- nothing is ever counted across a coverage gap or
  a coverage hole between periods.
* At a period boundary the *new* period's threshold is already in effect.
  When the temperature is strictly above the threshold on BOTH sides, the
  excursion continues as one; otherwise the old excursion ends at the
  boundary and/or a new one opens there (no double counting, no gap in the
  accounting).
* Exposed pieces touching at an ordinary shared reading merge into one
  excursion (a reading exactly on the threshold does not split it).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Excursion:
    """One continuous interval with temperature strictly above threshold."""

    start: Fraction  # epoch seconds
    end: Fraction  # epoch seconds
    degree_seconds: Fraction  # integral of (celsius - threshold) over [start, end]

    @property
    def duration_seconds(self) -> Fraction:
        return self.end - self.start

    @property
    def degree_minutes(self) -> Fraction:
        return self.degree_seconds / 60


@dataclass(frozen=True)
class Gap:
    """An interval with no trustworthy coverage (sampling gap)."""

    start: Fraction
    end: Fraction

    @property
    def duration_seconds(self) -> Fraction:
        return self.end - self.start


@dataclass(frozen=True)
class ExposureResult:
    excursions: list  # list[Excursion], time-ordered
    gaps: list  # list[Gap], time-ordered
    total_degree_seconds: Fraction

    @property
    def total_degree_minutes(self) -> Fraction:
        return self.total_degree_seconds / 60


@dataclass(frozen=True)
class _Piece:
    """Exposed part of one (sampling-segment x threshold-period) cell."""

    start: Fraction
    end: Fraction
    degree_seconds: Fraction
    start_strict: bool  # temperature strictly above the threshold at start
    end_strict: bool  # temperature strictly above the threshold at end


def _exposed_piece(
    s: Fraction,
    e: Fraction,
    vs: Fraction,
    ve: Fraction,
    threshold: Fraction,
) -> _Piece | None:
    """Exposed sub-interval of a linear trajectory v over [s, e].

    ``vs`` / ``ve`` are the (already interpolated) endpoint temperatures and
    ``threshold`` is constant across the cell.  Crossing points are solved
    exactly; equality with the threshold is not exposure.
    """
    a = vs - threshold  # excess at cell start
    b = ve - threshold  # excess at cell end

    if a <= 0 and b <= 0:
        return None  # never strictly above threshold

    if a > 0 and b > 0:
        # Whole cell exposed: trapezoid area.
        return _Piece(s, e, (a + b) * (e - s) / 2, True, True)

    # Exactly one end is above threshold: solve the crossing point exactly,
    # then integrate the exposed triangle.
    f = a / (a - b)  # fraction of the cell where v == threshold
    tc = s + f * (e - s)
    if a > 0:
        return _Piece(s, tc, a * (tc - s) / 2, True, False)
    return _Piece(tc, e, b * (e - tc) / 2, False, True)


def compute_exposure(
    points: list,  # list[(Fraction, Fraction)] sorted by time
    threshold: Fraction,
    max_interval: Fraction,
    threshold_periods: list | None = None,  # list[(start, end, threshold)]
) -> ExposureResult:
    pieces: list = []
    gaps: list = []

    if threshold_periods is None:
        # Single constant threshold for the whole transport.
        period_boundaries = frozenset()

        def threshold_at(_t: Fraction) -> Fraction:
            return threshold
    else:
        # Callers validate coverage; sort defensively so input order can
        # never change the result.
        ordered = sorted(threshold_periods, key=lambda p: p[0])
        period_starts = [p[0] for p in ordered]
        period_thresholds = [p[2] for p in ordered]
        # Interior boundaries only; nothing merges across the transport ends.
        period_boundaries = frozenset(p[1] for p in ordered[:-1])

        def threshold_at(t: Fraction) -> Fraction:
            # bisect_right: exactly at a boundary the NEW period's threshold
            # is already in effect.
            i = bisect_right(period_starts, t) - 1
            return period_thresholds[i]

    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        dt = t1 - t0
        if dt > max_interval:
            # Coverage gap: no interpolation across this interval.
            gaps.append(Gap(t0, t1))
            continue

        # Split this linear sampling segment at every period boundary inside
        # it; each resulting cell has one constant threshold.
        cuts = [t0]
        cuts.extend(b for b in period_boundaries if t0 < b < t1)
        cuts.append(t1)
        cuts.sort()

        for s, e in zip(cuts, cuts[1:]):
            tau = threshold_at(s)
            vs = v0 + (v1 - v0) * (s - t0) / dt
            ve = v0 + (v1 - v0) * (e - t0) / dt
            piece = _exposed_piece(s, e, vs, ve, tau)
            if piece is not None:
                pieces.append(piece)

    # Merge adjacent exposed pieces.
    merged: list = []
    for piece in pieces:
        if merged and merged[-1].end == piece.start:
            if piece.start in period_boundaries and not (
                merged[-1].end_strict and piece.start_strict
            ):
                # Period boundary: the excursion only continues when both
                # sides are strictly above their own threshold.  Equality on
                # either side closes/opens intervals at the boundary.
                merged.append(piece)
                continue
            last = merged[-1]
            merged[-1] = _Piece(
                last.start,
                piece.end,
                last.degree_seconds + piece.degree_seconds,
                last.start_strict,
                piece.end_strict,
            )
        else:
            merged.append(piece)

    excursions = [
        Excursion(p.start, p.end, p.degree_seconds) for p in merged
    ]
    total = sum((e.degree_seconds for e in excursions), Fraction(0))
    return ExposureResult(excursions, gaps, total)
