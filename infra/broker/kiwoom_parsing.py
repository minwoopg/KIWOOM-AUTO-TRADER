from __future__ import annotations

"""키움 REST 응답의 숫자 문자열 파싱 — `infra/broker` 하위 모듈들이
공유하는 의존성 없는(no-dependency) helper입니다.

2026-08-18 (GPT 리뷰 반영, 1P0.8-B.2 dependency cleanup): 원래
`KiwoomBroker._parse_abs_int()`(정적 메서드)로만 존재했는데,
`infra/broker/kiwoom_order_status.py`가 이 파싱 규칙을 재사용하려고
`from infra.broker.kiwoom_broker import KiwoomBroker`를 했습니다.
`kiwoom_order_status.py`는 스스로 "raw dict를 BrokerOrder로 바꾸는
순수 함수" 모듈이라고 선언했는데, 구체적인 HTTP `Broker` 구현
클래스에 의존하는 건 그 선언과 어긋났고, 더 실질적으로는 다음
1P0.8-C에서 `KiwoomBroker`가 `kiwoom_order_status.py`를 import하게
되면(실제 조회 결과를 `BrokerOrder`로 변환하기 위해) 아래처럼
순환 import가 됩니다.

    kiwoom_broker.py → kiwoom_order_status.py → kiwoom_broker.py

그래서 숫자 파싱 로직만 이 파일로 분리했습니다. `KiwoomBroker`와
`kiwoom_order_status.py` 둘 다 이 파일에만 의존하고 서로는 의존하지
않습니다. `KiwoomBroker._parse_abs_int()`는 하위 호환을 위해
그대로 남겨두되, 내부에서 이 함수를 호출하도록 위임합니다(로직
중복 없음).
"""

from typing import Any


def parse_abs_int(value: Any) -> int:
    """키움 숫자 문자열을 안전하게 정수로 바꿉니다.

    키움 응답은 종종 아래처럼 옵니다.
    - '000000000437250'
    - '-218750'
    - '+225000'
    - ''

    현재가/기준가/수량처럼 '크기'가 중요한 값은 절대값으로 써야 하므로
    여기서는 abs(int(...)) 형태로 처리합니다. 파싱할 수 없는 값은
    예외를 던지지 않고 0으로 fail-close합니다.
    """

    if value is None:
        return 0

    text = str(value).strip()
    if not text:
        return 0

    try:
        return abs(int(float(text)))
    except (ValueError, TypeError, OverflowError):
        return 0
