"""提醒默认本地时间, 相对接受时间与夏令时拒绝"""
from datetime import datetime, timezone
import time

import pytest

from satrap.core.group_chat.reminder_time import resolve_reminder_time


def test_relative_time_is_based_on_acceptance_not_platform_time():
    assert resolve_reminder_time({"after_seconds": 10}, 1000) == 1010
    assert resolve_reminder_time({"after_seconds": 31536000}, 1000) == 31537000


@pytest.mark.parametrize("values", [{}, {"due_at": "2026-10-04", "after_seconds": 10}, {"after_seconds": True},
                                      {"after_seconds": 9}, {"after_seconds": 31536001}, {"due_at": "2026-10-04"}])
def test_invalid_or_ambiguous_choices_rejected(values):
    with pytest.raises(ValueError):
        resolve_reminder_time(values, 1000)


def test_explicit_offsets_and_local_time_return_utc_deadlines():
    value = "2026-10-04T09:00:00+08:00"
    expected = datetime.fromisoformat(value).timestamp()
    assert resolve_reminder_time({"due_at": value}, expected - 60) == expected
    local = datetime(2026, 10, 4, 9, 0)
    expected_local = local.astimezone().timestamp()
    assert resolve_reminder_time({"due_at": "2026-10-04T09:00:00"}, expected_local - 60) == expected_local


def test_local_dst_fold_is_refused_with_explicit_candidates(monkeypatch):
    fields = (2026, 11, 1, 1, 30, 0)
    first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp()
    second = first + 3600
    monkeypatch.setattr(time, "mktime", lambda value: first if value[-1] == 1 else second)
    monkeypatch.setattr(time, "localtime", lambda value: time.struct_time((*fields, 0, 0, int(value == first))))
    with pytest.raises(ValueError, match="夏令时歧义"):
        resolve_reminder_time({"due_at": "2026-11-01T01:30:00"}, first - 60)


def test_local_dst_gap_is_refused(monkeypatch):
    monkeypatch.setattr(time, "mktime", lambda value: 12345)
    monkeypatch.setattr(time, "localtime", lambda value: time.struct_time((2026, 3, 8, 3, 30, 0, 0, 0, 1)))
    with pytest.raises(ValueError, match="不存在"):
        resolve_reminder_time({"due_at": "2026-03-08T02:30:00"}, 1000)
