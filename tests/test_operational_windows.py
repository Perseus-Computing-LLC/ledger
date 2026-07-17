import datetime as dt

from plutus_agent.savings import operational_window


def test_operational_windows_are_utc_and_bounded():
    now = dt.datetime(2026, 7, 17, 15, 30, tzinfo=dt.timezone.utc).timestamp()
    label, start, end = operational_window("today", now)
    assert label == "today"
    assert end == now
    assert end - start == 15.5 * 3600

    label, start, end = operational_window("24h", now)
    assert label == "last-24h"
    assert end - start == 86400

    label, start, end = operational_window("mtd", now)
    assert label == "mtd"
    assert dt.datetime.fromtimestamp(start, dt.timezone.utc).day == 1
    assert end == now

    label, start, end = operational_window("billing", now)
    assert label == "2026-07"
    assert end == now
