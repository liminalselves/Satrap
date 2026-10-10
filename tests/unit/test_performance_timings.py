import pytest

from satrap.core.utils.performance import PhaseTimings
from satrap.core.utils import performance


def test_timings_accumulate_failed_phases_without_suppressing_error(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(performance.time, 'perf_counter', lambda: now[0])
    timings = PhaseTimings()
    with timings.phase('install_ms'):
        now[0] = .1
    with pytest.raises(ValueError):
        with timings.phase('install_ms'):
            now[0] = .15
            raise ValueError('安装失败')
    now[0] = .2
    result = timings.to_payload()
    assert result == {'install_ms': 150.0, 'total_ms': 200.0}
    result['install_ms'] = -1
    assert timings.to_payload()['install_ms'] == 150.0
