from src.state_machine import TradingStateMachine


def test_safe_mode_auto_recover_predicate():
    # helper를 직접 테스트하기 위해 내부 staticmethod를 사용합니다.
    assert TradingStateMachine._should_auto_recover_safe_mode(
        safe_mode=True,
        error_count=0,
        now_ms=1_000_000,
        stable_since_ms=0,
        entry_pause_until_ms=0,
        recover_seconds=900,
        auto_recover=True,
    ) is False

    # 안정화 시간 경과 + 에러카운트 0 + pause 종료면 복구
    assert TradingStateMachine._should_auto_recover_safe_mode(
        safe_mode=True,
        error_count=0,
        now_ms=1_000_000,
        stable_since_ms=1_000_000 - 901_000,
        entry_pause_until_ms=0,
        recover_seconds=900,
        auto_recover=True,
    ) is True

    # 아직 pause가 남아있으면 복구하지 않음
    assert TradingStateMachine._should_auto_recover_safe_mode(
        safe_mode=True,
        error_count=0,
        now_ms=1_000_000,
        stable_since_ms=1_000_000 - 901_000,
        entry_pause_until_ms=1_000_001,
        recover_seconds=900,
        auto_recover=True,
    ) is False

    # 에러카운트가 남아있으면 복구하지 않음
    assert TradingStateMachine._should_auto_recover_safe_mode(
        safe_mode=True,
        error_count=2,
        now_ms=1_000_000,
        stable_since_ms=1_000_000 - 901_000,
        entry_pause_until_ms=0,
        recover_seconds=900,
        auto_recover=True,
    ) is False
