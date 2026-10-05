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
* Only temperatures *strictly* above the threshold count as exposure; a
  reading exactly equal to the threshold contributes nothing.
* Threshold crossings are solved exactly (linear interpolation), and the
  exposed sub-intervals plus their degree-minute areas (trapezoid/triangle
  integration of degrees above threshold over time) are accumulated.
* Exposed pieces touching at a shared reading merge into one excursion.

Threshold periods
-----------------
A shipment may use different temperature limits per phase (loading,
equilibration, steady transport).  Pass ``thresholds`` as a list of
``ThresholdPeriod`` covering the whole transport, adjacent and non-overlapping;
the periods need not be given in order (the caller may hand them over as
received).  Each reading segment is split exactly at every phase boundary
inside it and evaluated against the threshold of its own phase.  At a boundary
the *new* phase's threshold is in force for the boundary instant itself.

If two exposed pieces meet at a boundary the exposure stays a single excursion
(no double counting and no missing instant); if the temperature is not
strictly above the new threshold on the right side, the excursion ends at the
boundary, and when it later rises above that phase's threshold a new excursion
begins.  Degree-minute areas are integrated piecewise against each phase's
own threshold, never across a coverage gap.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class ThresholdPeriod:
    """One phase with its own temperature limit.

    The interval is half-open: ``[start, end)``.  A boundary instant uses the
    threshold of the period that starts there.
    """

    start: Fraction  # epoch seconds
    end: Fraction  # epoch seconds
    threshold: Fraction  # celsius for this phase


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


def _exposed_piece(
    t0: Fraction,
    v0: Fraction,
    t1: Fraction,
    v1: Fraction,
    threshold: Fraction,
):
    """Exposed part of one linear segment against a constant threshold.

    Returns ``(start, end, degree_seconds)`` of the strictly-above-threshold
    sub-interval, or ``None``.  ``t0 < t1`` and ``v0`` / ``v1`` are the exact
    endpoint temperatures (already interpolated when the segment was split at
    a phase boundary).
    """
    a = v0 - threshold  # excess above threshold at segment start
    b = v1 - threshold  # excess at segment end

    if a <= 0 and b <= 0:
        return None  # never strictly above threshold

    if a > 0 and b > 0:
        # Whole piece exposed: trapezoid area.
        return t0, t1, (a + b) * (t1 - t0) / 2

    # Exactly one end is above threshold: solve the crossing point exactly,
    # then integrate the exposed triangle.
    f = a / (a - b)  # fraction of the piece where v == threshold
    t_cross = t0 + f * (t1 - t0)
    if a > 0:
        return t0, t_cross, a * (t_cross - t0) / 2
    return t_cross, t1, b * (t1 - t_cross) / 2


def compute_exposure(
    points: list,  # list[(Fraction, Fraction)] sorted by time
    threshold: Fraction | None,
    max_interval: Fraction,
    thresholds: list | None = None,  # list[ThresholdPeriod], any input order
) -> ExposureResult:
    if thresholds is None and threshold is None:
        raise ValueError("provide either a constant threshold or threshold periods")
    if thresholds is not None:
        periods = sorted(thresholds, key=lambda p: (p.start, p.end))
        bounds = [p.start for p in periods]

        def period_at(t: Fraction) -> ThresholdPeriod:
            # bisect_right over the period starts: at a boundary instant the
            # period starting there (not the one ending there) is selected.
            return periods[min(bisect_right(bounds, t), len(periods)) - 1]
    else:
        periods = None

        def period_at(t: Fraction, _threshold=threshold) -> Fraction:
            return _threshold

    pieces: list = []
    gaps: list = []

    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        dt = t1 - t0
        if dt > max_interval:
            # Coverage gap: no interpolation across this interval.
            gaps.append(Gap(t0, t1))
            continue

        if periods is None:
            subs = [(t0, v0, t1, v1, threshold)]
        else:
            # Split at every phase boundary strictly inside the reading
            # segment.  The temperature at each split is interpolated on the
            # shared linear trajectory, and each sub-interval keeps its own
            # phase threshold.  Duplicate bounds (validated by the caller) are
            # excluded by the strict comparisons.
            subs = []
            ts = t0
            vs = v0
            for b in bounds:
                if t0 < b < t1:
                    vb = v0 + (v1 - v0) * (b - t0) / dt
                    subs.append((ts, vs, b, vb, period_at(ts).threshold))
                    ts, vs = b, vb
            subs.append((ts, vs, t1, v1, period_at(ts).threshold))

        for st, sv, et, ev, thr in subs:
            piece = _exposed_piece(st, sv, et, ev, thr)
            if piece is not None:
                ps, pe, parea = piece
                pieces.append(Excursion(ps, pe, parea))

    # Merge pieces that touch at a shared point - either a shared reading
    # (both sides exposed) or a phase boundary where the temperature is
    # strictly above both the outgoing and the incoming threshold.  A coverage
    # gap never reaches this list because gap segments are skipped wholesale,
    # so exposure never continues across one.
    merged: list = []
    for piece in pieces:
        if merged and merged[-1].end == piece.start:
            last = merged[-1]
            merged[-1] = Excursion(
                last.start, piece.end, last.degree_seconds + piece.degree_seconds
            )
        else:
            merged.append(piece)

    total = sum((p.degree_seconds for p in merged), Fraction(0))
    return ExposureResult(merged, gaps, total)
