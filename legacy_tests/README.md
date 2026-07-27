# legacy_tests/

2026-07-27, GPT 코드리뷰로 발견 — 예전 `tests/` 디렉토리에 있던 파일들을
여기로 옮겼습니다. 이 디렉토리의 파일들은 **현재 정식 회귀 테스트가
아닙니다.** `run_regression_tests.py`나 CI에서 실행하지 않습니다.

## 왜 여기로 옮겼는가

원래 `tests/` 디렉토리에 프로젝트 루트와 같은 이름의 파일이 4개
있었습니다(`test_entry_watch.py`, `test_position_priority_order.py`,
`test_run_once_integration.py`, `test_sold_today_and_reentry_fix.py`).
pytest가 두 디렉토리를 함께 수집하면 `import file mismatch`로 실패하고,
그중 `tests/test_sold_today_and_reentry_fix.py`는 이후 정책 변경
(trades.csv 파일 미존재 시 fail-open → fail-close 전환, CHANGELOG_v1.5.md
7.27절 등)에 맞춰 갱신되지 않은 구버전이라 실제로 6건 중 2건이
실패했습니다(재현 확인).

`test_breakout_strategy.py`는 초기 개발 단계에 pytest 클래스 스타일로
작성된 파일로, 이후 `StrategyConfig`에 트레일링 관련 필드가 추가되면서
시그니처가 맞지 않아 2건 모두 `TypeError`로 실패합니다(재현 확인).

## 현재 이 파일들이 검증하려던 내용

- 매수/매도 신호 생성 기본 동작 (`test_breakout_strategy.py`)
- 재진입 제한, 1일1회 매수 게이트 (`test_sold_today_and_reentry_fix.py`)
- 종목 우선순위 처리 (`test_position_priority_order.py`)
- entry_watch 기본 동작 (`test_entry_watch.py`)
- run_once 통합 흐름 (`test_run_once_integration.py`)

이 내용들은 프로젝트 루트의 최신 테스트 파일(같은 이름 또는
`test_position_lifecycle_*.py`, `test_risk_manager_*.py` 등)에서 이미
더 정확하고 최신 상태로 검증되고 있습니다 — 커버리지 손실은 없습니다.

## 정식 회귀 테스트는 어디에

프로젝트 루트의 `test_*.py` 25개 파일이 공식 회귀 스위트입니다.
`python run_regression_tests.py`로 전체 실행하세요.
