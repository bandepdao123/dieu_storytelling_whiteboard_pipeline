import pytest
from du_pipeline.policies import DurationPolicy, validate_timing, TimingMismatch, required_approval, SchedulerConfig


def test_duration_policy_and_soft_bounds():
    p = DurationPolicy()
    assert p.bounds_at(0) == (5, 8)
    assert p.bounds_at(180) == (10, 20)
    assert p.choose(0, semantic_seconds=9) == 8
    assert p.choose(181, semantic_seconds=11) == 11


def test_strict_audio_srt_mismatch_blocks():
    with pytest.raises(TimingMismatch):
        validate_timing(10_000, [(0, 4_000, "a"), (3_900, 10_000, "b")])
    with pytest.raises(TimingMismatch):
        validate_timing(10_000, [(0, 8_000, "a")])
    assert validate_timing(10_000, [(0, 5_000, "a"), (5_000, 9_700, "b")])


def test_approval_and_resource_defaults():
    assert required_approval("S005", False)
    assert required_approval("S010", True)
    assert not required_approval("S006", False)
    c = SchedulerConfig()
    assert c.cpu_limit == 4 and c.memory_mb <= 3072 and c.image_workers == 1
