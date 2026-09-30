"""Bitemporal append-only store for capacity rules.

Two time axes are tracked independently:

* ``window``      - the *valid* time the rule applies on the factory floor
                    (a half-open interval).
* ``recorded_at`` - the *transaction* time the rule entered the system.

A historical query at ``as_of`` replays only rules recorded at or before that
instant.  Nothing stored is ever mutated or deleted; a later change to a rule
is written as a new record that marks the old one ``superseded_at``.  Backdating
a rule's *window* is allowed (real corrections happen late), but it can never
edit what an earlier ``as_of`` query returned.  ``drift`` makes that divergence
explicit instead of letting a past answer silently change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fabweave.capacity.calendar import CapacityRule, DayCapacity, RuleKind, resolve_day
from fabweave.core.ids import validate_id
from fabweave.core.timebox import TimeBox


@dataclass(frozen=True, slots=True)
class _Entry:
    rule: CapacityRule
    line_id: str
    recorded_at: int
    superseded_at: int | None
    note: str


@dataclass(frozen=True, slots=True)
class HistoryDrift:
    """How a past day's answer differs under current knowledge."""

    line_id: str
    day: TimeBox
    as_of: int
    historical_minutes: int
    current_minutes: int
    changed: bool
    added_rules: tuple[str, ...] = field(default_factory=tuple)
    removed_rules: tuple[str, ...] = field(default_factory=tuple)


class CapacityCalendar:
    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._line_versions: dict[str, int] = {}

    # -- writes -----------------------------------------------------------

    def add_rule(
        self,
        line_id: str,
        kind: str,
        windows: list[TimeBox] | list[tuple[int, int]],
        recorded_at: int,
        *,
        rule_id: str | None = None,
        note: str = "",
    ) -> CapacityRule:
        """Append a rule.  A later-dated addition is a new version, never an edit.

        Backdated *windows* (a rule entered after the day passed) are permitted;
        backdating the *recorded_at* of an existing rule is impossible here.
        """

        validate_id(line_id)
        if kind not in RuleKind.ALL:
            raise ValueError("unknown capacity rule kind")
        if not windows:
            raise ValueError("a capacity rule needs at least one window")
        normalized = tuple(
            window if isinstance(window, TimeBox) else TimeBox(window[0], window[1])
            for window in windows
        )
        version = self._line_versions.get(line_id, 0) + 1
        self._line_versions[line_id] = version
        rid = rule_id if rule_id is not None else f"RULE-{line_id}-{version:04d}"
        validate_id(rid)
        rule = CapacityRule(rule_id=rid, kind=kind, windows=normalized, rule_version=version)
        self._entries.append(_Entry(rule, line_id, recorded_at, None, note))
        return rule

    def correct_rule(
        self,
        line_id: str,
        rule_id: str,
        corrected_at: int,
        *,
        kind: str | None = None,
        windows: list[TimeBox] | list[tuple[int, int]] | None = None,
        note: str = "correction",
    ) -> tuple[CapacityRule, CapacityRule]:
        """Replace a rule.  The old record is tombstoned, not overwritten.

        Queries at an ``as_of`` before ``corrected_at`` still see the old rule;
        later queries see the new one.  Both are retained for audit.
        """

        target = self._latest_record(line_id, rule_id)
        if target.superseded_at is not None:
            raise ValueError("rule already superseded; correct its current version")
        new_kind = kind if kind is not None else target.rule.kind
        new_windows = (
            tuple(
                window if isinstance(window, TimeBox) else TimeBox(window[0], window[1])
                for window in windows
            )
            if windows is not None
            else target.rule.windows
        )
        version = self._line_versions.get(line_id, 0) + 1
        self._line_versions[line_id] = version
        new_rule = CapacityRule(rule_id, new_kind, new_windows, version)

        old_index = next(
            i for i, entry in enumerate(self._entries)
            if entry.line_id == line_id and entry.rule.rule_id == rule_id
            and entry.superseded_at is None
        )
        old_entry = self._entries[old_index]
        self._entries[old_index] = _Entry(
            old_entry.rule, old_entry.line_id, old_entry.recorded_at, corrected_at, old_entry.note
        )
        self._entries.append(_Entry(new_rule, line_id, corrected_at, None, note))
        return old_entry.rule, new_rule

    # -- reads ------------------------------------------------------------

    def rules_for(self, line_id: str, day: TimeBox, as_of: int) -> list[CapacityRule]:
        """Rules visible for the day exactly as known at ``as_of``."""

        return [
            entry.rule
            for entry in self._entries
            if entry.line_id == line_id
            and entry.recorded_at <= as_of
            and (entry.superseded_at is None or as_of < entry.superseded_at)
            and any(window.intersection(day) for window in entry.rule.windows)
        ]

    def day_capacity(self, line_id: str, day: TimeBox, as_of: int) -> DayCapacity:
        """Resolve one line/date using the rule version known at ``as_of``."""

        return resolve_day(line_id, day, self.rules_for(line_id, day, as_of))

    def current_day_capacity(self, line_id: str, day: TimeBox) -> DayCapacity:
        """Resolve under the newest knowledge (equivalent to ``as_of = now``)."""

        return self.day_capacity(line_id, day, self._horizon())

    def drift(self, line_id: str, day: TimeBox, as_of: int) -> HistoryDrift:
        """Compare an answer given at ``as_of`` with today's answer.

        This is the guard against quietly rewritten history: if late rules
        changed a past day, ``changed`` is true and the responsible rule ids
        are listed for review.
        """

        historical = self.day_capacity(line_id, day, as_of)
        current = self.current_day_capacity(line_id, day)
        old_ids = {rule.rule_id for rule in historical.governing}
        new_ids = {rule.rule_id for rule in current.governing}
        return HistoryDrift(
            line_id=line_id,
            day=day,
            as_of=as_of,
            historical_minutes=historical.available_minutes,
            current_minutes=current.available_minutes,
            changed=(
                historical.available_minutes != current.available_minutes
                or historical.available_windows != current.available_windows
            ),
            added_rules=tuple(sorted(new_ids - old_ids)),
            removed_rules=tuple(sorted(old_ids - new_ids)),
        )

    # -- internals --------------------------------------------------------

    def _latest_record(self, line_id: str, rule_id: str) -> _Entry:
        for entry in reversed(self._entries):
            if entry.line_id == line_id and entry.rule.rule_id == rule_id:
                return entry
        raise LookupError("unknown rule")

    def _horizon(self) -> int:
        if not self._entries:
            return 0
        return max(
            max(entry.recorded_at, entry.superseded_at or entry.recorded_at)
            for entry in self._entries
        )
