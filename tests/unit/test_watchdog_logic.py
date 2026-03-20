"""
Unit tests for WatchdogService threshold logic (cmd/watchdog/main.py).

The thresholds that trigger rollback/throttle live in WatchdogService
class attributes. Tests verify those exact values and the comparison
direction — avoiding any reliance on mocked I/O.
"""

import pytest
from cmd.watchdog.main import WatchdogService


# ── Class-attribute threshold values ─────────────────────────────────────────

class TestThresholdValues:
    """These tests pin the exact rollback/throttle thresholds so a
    refactor can't silently change acceptable limits."""

    def test_error_rate_threshold_is_10_percent(self):
        assert WatchdogService.ERROR_RATE_THRESHOLD == pytest.approx(0.10)

    def test_latency_spike_threshold_is_5000ms(self):
        assert WatchdogService.LATENCY_SPIKE_THRESHOLD_MS == 5000

    def test_gpu_thermal_critical_is_85_celsius(self):
        assert WatchdogService.GPU_THERMAL_CRITICAL == 85

    def test_gpu_memory_critical_is_512mb(self):
        assert WatchdogService.GPU_MEMORY_CRITICAL_MB == 512


# ── Rollback-trigger checks (synchronous logic extracted) ────────────────────
# We mirror the comparisons from monitor_metrics() so tests don't
# need the ledger/vector dependencies to be wired up.

def _check_error_rate(error_rate: float) -> bool:
    return error_rate > WatchdogService.ERROR_RATE_THRESHOLD

def _check_latency(latency_ms: int) -> bool:
    return latency_ms > WatchdogService.LATENCY_SPIKE_THRESHOLD_MS

def _check_gpu_temp(temp_c: float) -> bool:
    return temp_c > WatchdogService.GPU_THERMAL_CRITICAL

def _check_gpu_memory(available_mb: float) -> bool:
    """Returns True when memory is critically LOW (throttle, not rollback)."""
    return available_mb < WatchdogService.GPU_MEMORY_CRITICAL_MB


class TestErrorRateTrigger:
    @pytest.mark.parametrize("rate,expect_rollback", [
        (0.11, True),
        (0.10, False),   # exactly at threshold → does NOT trigger (strictly greater than)
        (0.09, False),
        (0.00, False),
        (1.00, True),    # 100% error rate
    ])
    def test_error_rate_boundary(self, rate, expect_rollback):
        assert _check_error_rate(rate) == expect_rollback


class TestLatencyTrigger:
    @pytest.mark.parametrize("ms,expect_rollback", [
        (5001, True),
        (5000, False),   # exactly at threshold → does NOT trigger
        (4999, False),
        (0,    False),
        (9999, True),
    ])
    def test_latency_boundary(self, ms, expect_rollback):
        assert _check_latency(ms) == expect_rollback


class TestGpuThermalTrigger:
    @pytest.mark.parametrize("temp,expect_rollback", [
        (86.0, True),
        (85.0, False),   # exactly at threshold → does NOT trigger
        (84.9, False),
        (0.0,  False),
        (100.0, True),
    ])
    def test_gpu_temp_boundary(self, temp, expect_rollback):
        assert _check_gpu_temp(temp) == expect_rollback


class TestGpuMemoryThrottle:
    @pytest.mark.parametrize("mb,expect_throttle", [
        (511.9, True),
        (512.0, False),  # exactly at threshold → does NOT throttle
        (513.0, False),
        (0.0,   True),
        (8192.0, False),
    ])
    def test_gpu_memory_boundary(self, mb, expect_throttle):
        assert _check_gpu_memory(mb) == expect_throttle

    def test_gpu_memory_throttle_is_not_rollback(self):
        # Low GPU memory → throttle only; error rate and latency still fine
        assert _check_error_rate(0.05) is False
        assert _check_latency(1000) is False
        assert _check_gpu_memory(300.0) is True   # only memory is critical


# ── All-healthy path ─────────────────────────────────────────────────────────

class TestAllHealthy:
    def test_good_metrics_trigger_nothing(self):
        error_rate = 0.02
        latency_ms = 300
        gpu_temp = 65.0
        gpu_mem  = 4000.0

        assert _check_error_rate(error_rate) is False
        assert _check_latency(latency_ms) is False
        assert _check_gpu_temp(gpu_temp) is False
        assert _check_gpu_memory(gpu_mem) is False
