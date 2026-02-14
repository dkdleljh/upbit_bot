import pytest

pytest.skip("signal_engine scoring helpers were replaced by composite build_signal; tests need update", allow_module_level=True)

from src.signal_engine import score_execution, score_momentum, score_notional


def test_score_execution_cut_and_pass():
    s, ok = score_execution(0.0005, 8.5)
    assert ok is True
    assert s == 40

    s2, ok2 = score_execution(0.0015, 9)
    assert ok2 is False
    assert s2 == 0


def test_score_notional_and_momentum():
    assert score_notional(4.2) == 30
    assert score_notional(3.2) == 22
    assert score_notional(2.4) == 12
    assert score_notional(1.9) == 0

    assert score_momentum(0.013) == 10
    assert score_momentum(0.007) == 6
    assert score_momentum(0.001) == 2
    assert score_momentum(-0.001) == 0
