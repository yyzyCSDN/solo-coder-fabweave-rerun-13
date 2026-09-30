from fabweave.capacity import (
    OVERRIDE_ORDER,
    CapacityCalendar,
    CapacityRule,
    RuleKind,
    resolve_day,
    subtract_windows,
)
from fabweave.core.timebox import TimeBox

LINE = "LINE-A"
MON = TimeBox(0, 1440)


def r(rid, kind, windows, version=1):
    return CapacityRule(rid, kind, tuple(TimeBox(*w) for w in windows), version)


def test_precedence_chain_is_documented_order():
    assert OVERRIDE_ORDER == (
        RuleKind.SHUTDOWN, RuleKind.HOLIDAY, RuleKind.SHORTENED,
        RuleKind.BASE, RuleKind.OVERTIME,
    )


def test_half_open_adjacent_windows_do_not_double_count():
    day = resolve_day(LINE, MON, [r("RULE-BASE", RuleKind.BASE, [(0, 480), (480, 960)])])
    assert day.available_windows == (TimeBox(0, 960),)
    assert day.available_minutes == 960
    assert subtract_windows([TimeBox(0, 60)], [TimeBox(30, 60)]) == [TimeBox(0, 30)]


def test_base_only_defaults_closed_outside_shift():
    day = resolve_day(LINE, MON, [r("RULE-BASE", RuleKind.BASE, [(480, 960)])])
    assert day.available_minutes == 480
    assert day.available_windows == (TimeBox(480, 960),)
    assert day.minutes_by_kind()[RuleKind.BASE] == 480


def test_overtime_adds_only_outside_base():
    day = resolve_day(
        LINE, MON,
        [r("RULE-BASE", RuleKind.BASE, [(480, 960)]),
         r("RULE-OT", RuleKind.OVERTIME, [(960, 1080)])],
    )
    assert day.available_windows == (TimeBox(480, 1080),)
    assert day.available_minutes == 600
    assert day.minutes_by_kind()[RuleKind.OVERTIME] == 120


def test_holiday_overrides_base_and_overtime():
    day = resolve_day(
        LINE, MON,
        [r("RULE-BASE", RuleKind.BASE, [(480, 960)]),
         r("RULE-OT", RuleKind.OVERTIME, [(960, 1080)]),
         r("RULE-HOL", RuleKind.HOLIDAY, [(0, 1440)])],
    )
    assert day.available_minutes == 0
    assert day.available_windows == ()


def test_shutdown_beats_holiday_but_only_on_overlap():
    # Morning holiday, afternoon shutdown, base running all day: closures win
    # their minutes (shutdown wins the overlap), base reopens after 900.
    day = resolve_day(
        LINE, MON,
        [r("RULE-BASE", RuleKind.BASE, [(0, 1440)]),
         r("RULE-HOL", RuleKind.HOLIDAY, [(0, 720)]),
         r("RULE-SD", RuleKind.SHUTDOWN, [(600, 900)])],
    )
    assert day.available_windows == (TimeBox(900, 1440),)
    assert day.available_minutes == 540
    by_kind = day.minutes_by_kind()
    assert by_kind[RuleKind.SHUTDOWN] == 300
    assert by_kind[RuleKind.HOLIDAY] == 600
    assert by_kind[RuleKind.BASE] == 540
    assert day.closed_windows == (TimeBox(0, 900),)


def test_shutdown_partially_erases_overtime():
    day = resolve_day(
        LINE, MON,
        [r("RULE-OT", RuleKind.OVERTIME, [(0, 240)]),
         r("RULE-SD", RuleKind.SHUTDOWN, [(60, 120)])],
    )
    assert day.available_windows == (TimeBox(0, 60), TimeBox(120, 240))
    assert day.available_minutes == 180


def test_shortened_replaces_base_but_evening_overtime_still_adds():
    day = resolve_day(
        LINE, MON,
        [r("RULE-BASE", RuleKind.BASE, [(480, 960)]),
         r("RULE-SHORT", RuleKind.SHORTENED, [(480, 720)]),
         r("RULE-OT", RuleKind.OVERTIME, [(720, 840)])],
    )
    assert day.available_windows == (TimeBox(480, 840),)
    assert day.available_minutes == 360


def test_shortened_overlapping_overtime_minutes_lose():
    # Overtime claimed inside the shortened window adds nothing there: shortened
    # already keeps the line open, and shortened outranks overtime on the margin.
    day = resolve_day(
        LINE, MON,
        [r("RULE-SHORT", RuleKind.SHORTENED, [(480, 720)]),
         r("RULE-OT", RuleKind.OVERTIME, [(600, 780)])],
    )
    assert day.available_windows == (TimeBox(480, 780),)
    assert day.available_minutes == 300
    by_kind = day.minutes_by_kind()
    assert by_kind[RuleKind.SHORTENED] == 240
    assert by_kind[RuleKind.OVERTIME] == 60


def test_two_shortened_rules_same_day_is_rejected():
    try:
        resolve_day(
            LINE, MON,
            [r("RULE-S1", RuleKind.SHORTENED, [(480, 720)]),
             r("RULE-S2", RuleKind.SHORTENED, [(800, 900)])],
        )
    except ValueError:
        return
    raise AssertionError("expected ambiguous shortened day to be rejected")


def test_historical_query_uses_rules_known_then():
    cal = CapacityCalendar()
    cal.add_rule(LINE, RuleKind.BASE, [(480, 960)], recorded_at=100)
    as_of_then = 100
    # At time 100 the day had the full base shift.
    assert cal.day_capacity(LINE, MON, as_of_then).available_minutes == 480

    # A rule recorded later (a late holiday entry) must not change that answer.
    cal.add_rule(LINE, RuleKind.HOLIDAY, [(0, 1440)], recorded_at=500)
    assert cal.day_capacity(LINE, MON, as_of_then).available_minutes == 480
    assert cal.current_day_capacity(LINE, MON).available_minutes == 0


def test_backdated_window_does_not_rewrite_past_query():
    cal = CapacityCalendar()
    cal.add_rule(LINE, RuleKind.BASE, [(480, 960)], recorded_at=100)
    # Entered on day 5, but valid on the earlier day 1: backfilling is allowed.
    cal.add_rule(LINE, RuleKind.SHUTDOWN, [(480, 960)], recorded_at=500)
    drift = cal.drift(LINE, MON, as_of=100)
    assert drift.changed is True
    assert drift.historical_minutes == 480
    assert drift.current_minutes == 0
    assert "RULE-LINE-A-0002" in drift.added_rules
    # Yet the old answer itself is still reproducible.
    assert cal.day_capacity(LINE, MON, 100).available_minutes == 480


def test_correction_supersedes_without_destroying_old_version():
    cal = CapacityCalendar()
    cal.add_rule(LINE, RuleKind.BASE, [(480, 960)], recorded_at=100, rule_id="RULE-SHIFT")
    cal.correct_rule(LINE, "RULE-SHIFT", corrected_at=300,
                     windows=[(540, 960)], note="shift start moved")
    assert cal.day_capacity(LINE, MON, 250).available_minutes == 480
    assert cal.day_capacity(LINE, MON, 300).available_minutes == 420
    drift = cal.drift(LINE, MON, 250)
    assert drift.changed is True
    assert drift.removed_rules == ()  # same id, new version


def test_no_drift_when_nothing_changed():
    cal = CapacityCalendar()
    cal.add_rule(LINE, RuleKind.BASE, [(480, 960)], recorded_at=100)
    assert cal.drift(LINE, MON, 100).changed is False


def test_no_base_means_only_explicit_overtime_is_open():
    day = resolve_day(LINE, MON, [r("RULE-OT", RuleKind.OVERTIME, [(1200, 1320)])])
    assert day.available_windows == (TimeBox(1200, 1320),)
    assert day.available_minutes == 120
    # Everything else is simply unscheduled, not a closure.
    assert day.closed_windows == ()


def test_overtime_inside_base_adds_nothing():
    day = resolve_day(
        LINE, MON,
        [r("RULE-BASE", RuleKind.BASE, [(0, 720)]),
         r("RULE-OT", RuleKind.OVERTIME, [(100, 200)])],
    )
    assert day.available_windows == (TimeBox(0, 720),)
    assert day.minutes_by_kind()[RuleKind.OVERTIME] == 0
    assert day.minutes_by_kind()[RuleKind.BASE] == 720


def test_window_crossing_midnight_is_clipped_half_open():
    # Rule from 23:00 to 01:00 next day; query only this day's [0,1440).
    day = resolve_day(
        LINE, MON,
        [r("RULE-SD", RuleKind.SHUTDOWN, [(1380, 1500)]),
         r("RULE-BASE", RuleKind.BASE, [(0, 1440)])],
    )
    assert day.closed_windows == (TimeBox(1380, 1440),)
    assert day.available_windows == (TimeBox(0, 1380),)
    assert day.available_minutes == 1380


def test_as_of_boundary_is_inclusive():
    cal = CapacityCalendar()
    cal.add_rule(LINE, RuleKind.SHUTDOWN, [(0, 1440)], recorded_at=500)
    assert cal.rules_for(LINE, MON, 499) == []
    assert len(cal.rules_for(LINE, MON, 500)) == 1


def test_stacked_same_day_all_four_rules():
    cal = CapacityCalendar()
    cal.add_rule(LINE, RuleKind.BASE, [(480, 960)], recorded_at=1)
    cal.add_rule(LINE, RuleKind.HOLIDAY, [(0, 1080)], recorded_at=2)
    cal.add_rule(LINE, RuleKind.SHORTENED, [(480, 600)], recorded_at=3)
    cal.add_rule(LINE, RuleKind.OVERTIME, [(960, 1200)], recorded_at=4)
    cal.add_rule(LINE, RuleKind.SHUTDOWN, [(0, 720)], recorded_at=5)
    day = cal.current_day_capacity(LINE, MON)
    # holiday closes [720,1080); overtime survives only [1080,1200).
    assert day.available_windows == (TimeBox(1080, 1200),)
    assert day.available_minutes == 120
    governing_kinds = {rule.kind for rule in day.governing}
    assert governing_kinds == {
        RuleKind.BASE, RuleKind.HOLIDAY, RuleKind.SHORTENED,
        RuleKind.OVERTIME, RuleKind.SHUTDOWN,
    }


def _run_all():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"{len(tests)} capacity tests passed")


if __name__ == "__main__":
    _run_all()
