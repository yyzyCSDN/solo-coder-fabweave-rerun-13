"""Resolution of one day's available production minutes.

Four rule kinds may be stacked onto the same day and their windows may
overlap freely.  Conflict is settled by an explicit, documented precedence
chain rather than by insertion order or chance:

    shutdown  >  holiday  >  shortened  >  base  >  overtime

Base outranks overtime on their overlap, so temporary overtime only adds
minutes the standing schedule did not already provide.

Minutes are computed on half-open intervals :class:`fabweave.core.timebox.TimeBox`
(``[start, end)``), so adjacent windows never double-count a boundary minute.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fabweave.core.ids import validate_id
from fabweave.core.timebox import TimeBox, merge_timeboxes


class RuleKind:
    BASE = "base"
    OVERTIME = "overtime"
    SHORTENED = "shortened"
    HOLIDAY = "holiday"
    SHUTDOWN = "shutdown"

    ALL = (BASE, OVERTIME, SHORTENED, HOLIDAY, SHUTDOWN)


# Four rule kinds stack on one day. When their windows overlap, the winner on
# each minute follows this documented chain (lower rank wins); ties break by
# rule id.  Base outranks overtime so overtime only ever contributes minutes the
# schedule did not already cover, while closures and a shortened day still beat
# both.  The order is the "who covers whom" contract - never insertion order.
_PRECEDENCE = {
    RuleKind.SHUTDOWN: 0,
    RuleKind.HOLIDAY: 1,
    RuleKind.SHORTENED: 2,
    RuleKind.BASE: 3,
    RuleKind.OVERTIME: 4,
}

# Documentation-facing override chain, strongest coverage first.  Base is
# listed before overtime because base working time wins their overlap.
OVERRIDE_ORDER = (
    RuleKind.SHUTDOWN,
    RuleKind.HOLIDAY,
    RuleKind.SHORTENED,
    RuleKind.BASE,
    RuleKind.OVERTIME,
)


@dataclass(frozen=True, slots=True)
class CapacityRule:
    """A rule whose half-open window contributes to some day.

    ``kind`` decides precedence.  ``windows`` are absolute half-open
    intervals clipped to the queried day; they may run past midnight.
    Every rule carries the rule-set version it was introduced under so a
    resolved day can always be traced back to the rules in force.
    """

    rule_id: str
    kind: str
    windows: tuple[TimeBox, ...]
    rule_version: int

    def __post_init__(self) -> None:
        validate_id(self.rule_id)
        if self.kind not in RuleKind.ALL:
            raise ValueError("unknown capacity rule kind")
        if not self.windows:
            raise ValueError("a capacity rule needs at least one window")
        if self.rule_version <= 0:
            raise ValueError("rule version must be positive")
        object.__setattr__(self, "windows", tuple(merge_timeboxes(sorted(self.windows))))
        if any(window.duration <= 0 for window in self.windows):
            raise ValueError("rule windows must hold time")


@dataclass(frozen=True, slots=True)
class DayCapacity:
    line_id: str
    day: TimeBox
    available_windows: tuple[TimeBox, ...]
    available_minutes: int
    # The single rule owning each minute of the day that any rule covers,
    # including closed minutes.  Minutes with no covering rule are implicit
    # non-production time and appear nowhere here.
    winner_by_window: tuple[tuple[TimeBox, CapacityRule], ...] = field(default_factory=tuple)
    closed_windows: tuple[TimeBox, ...] = field(default_factory=tuple)
    governing: tuple[CapacityRule, ...] = field(default_factory=tuple)

    def minutes_by_kind(self) -> dict[str, int]:
        """Minutes owned per kind across all covered (open and closed) minutes."""

        totals: dict[str, int] = {kind: 0 for kind in RuleKind.ALL}
        for window, rule in self.winner_by_window:
            totals[rule.kind] += window.duration
        return totals


def subtract_windows(positive: list[TimeBox], negative: list[TimeBox]) -> list[TimeBox]:
    """Set difference of half-open windows without double-counting."""

    remainder: list[TimeBox] = []
    for segment in positive:
        cuts = sorted((window.intersection(segment) for window in negative if window.intersection(segment)),
                      key=lambda window: window.start)
        cursor = segment.start
        for cut in cuts:
            if cut.start > cursor:
                remainder.append(TimeBox(cursor, cut.start))
            cursor = max(cursor, cut.end)
        if cursor < segment.end:
            remainder.append(TimeBox(cursor, segment.end))
    return merge_timeboxes(remainder)


def _clip(windows: tuple[TimeBox, ...], day: TimeBox) -> list[TimeBox]:
    return [part for window in windows if (part := window.intersection(day))]


def _winning_segments(rules: list[CapacityRule], day: TimeBox) -> list[tuple[TimeBox, CapacityRule]]:
    """Sweep the day minute by minute boundary, assigning each segment a winner.

    Only window endpoints can change the winner, so we cut the day there and
    pick the highest-precedence active rule (ties broken by rule id).
    """

    boundaries = {day.start, day.end}
    active: list[tuple[TimeBox, CapacityRule]] = []
    for rule in rules:
        for window in _clip(rule.windows, day):
            active.append((window, rule))
            boundaries.add(window.start)
            boundaries.add(window.end)

    segments: list[tuple[TimeBox, CapacityRule]] = []
    for start, end in zip(sorted(boundaries), sorted(boundaries)[1:]):
        if start >= end:
            continue
        probe = start  # half-open: the owner at ``start`` owns [start, end)
        covering = [rule for window, rule in active if window.contains(probe)]
        if not covering:
            continue
        winner = min(covering, key=lambda rule: (_PRECEDENCE[rule.kind], rule.rule_id))
        segments.append((TimeBox(start, end), winner))

    # Re-join adjacent segments that resolved to the same rule.
    merged: list[tuple[TimeBox, CapacityRule]] = []
    for window, rule in segments:
        if merged and merged[-1][1] == rule and merged[-1][0].end == window.start:
            merged[-1] = (TimeBox(merged[-1][0].start, window.end), rule)
        else:
            merged.append((window, rule))
    return merged


def resolve_day(line_id: str, day: TimeBox, rules: list[CapacityRule]) -> DayCapacity:
    """Resolve available minutes for one line/date from every stacked rule.

    Closure kinds (shutdown/holiday) erase capacity on every minute they win;
    a shortened schedule masks the whole day's base; base beats overtime on
    overlap so temporary overtime only adds minutes the schedule left closed.
    """

    validate_id(line_id)
    present = [rule for rule in rules if _clip(rule.windows, day)]
    by_kind: dict[str, list[CapacityRule]] = {kind: [] for kind in RuleKind.ALL}
    for rule in present:
        by_kind[rule.kind].append(rule)

    # Two competing shortened schedules for one day would make "the" shortened
    # day ambiguous. Closures and additive overtime stack safely; base windows
    # merge as a split shift.
    shortened_rules = by_kind[RuleKind.SHORTENED]
    shortened_windows = merge_timeboxes(
        [part for rule in shortened_rules for part in _clip(rule.windows, day)]
    )
    if len({rule.rule_id for rule in shortened_rules}) > 1 and shortened_windows:
        raise ValueError("multiple shortened-shift rules cover the same day")

    # The winner sweep is the single source of truth: on each minute the
    # highest-precedence covering rule owns it. A shortened schedule masks base
    # on the whole day, so base cannot win any minute when one is present.
    masked = present if not shortened_windows else [
        rule for rule in present if rule.kind != RuleKind.BASE
    ]
    winners = _winning_segments(masked, day)
    closure_kinds = {RuleKind.SHUTDOWN, RuleKind.HOLIDAY}

    open_segments = [(w, rule) for w, rule in winners if rule.kind not in closure_kinds]
    closed_segments = [(w, rule) for w, rule in winners if rule.kind in closure_kinds]
    available = merge_timeboxes([w for w, _ in open_segments])
    closed = merge_timeboxes([w for w, _ in closed_segments])

    governing = tuple(sorted(present, key=lambda rule: (_PRECEDENCE[rule.kind], rule.rule_id)))
    return DayCapacity(
        line_id=line_id,
        day=day,
        available_windows=tuple(available),
        available_minutes=sum(window.duration for window in available),
        winner_by_window=tuple(open_segments + closed_segments),
        closed_windows=tuple(closed),
        governing=governing,
    )
