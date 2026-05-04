# Kiwoom Auto Trader ver1 상세 설계

## 1. 목표
- 파이썬으로 빠르게 자동매매 ver1을 만든다.
- 나중에 자바로 이식하기 쉬운 구조를 유지한다.
- 첫 단계는 키움 REST **모의투자**를 기준으로 안정적으로 붙인다.

## 2. 핵심 원칙
- 전략 로직과 증권사 API 코드를 분리한다.
- 키움 응답 JSON을 내부 모델로 변환해서 사용한다.
- 민감 정보는 `.env`에서 읽는다.
- 처음에는 종목 수를 적게 하고 주문도 작게 간다.

## 3. 폴더 구조
- `app/` : 실행 진입점
- `config/` : 설정 로드
- `domain/` : 전략, 리스크, 서비스, 내부 모델
- `infra/broker/` : 키움/모의 브로커 구현
- `infra/storage/` : 로그/상태 저장
- `scripts/` : 단독 테스트 파일
- `tests/` : 단위 테스트

## 4. KiwoomBroker 실제 연결 범위
현재 `KiwoomBroker`에 반영된 기능:
- `authenticate()` : `/oauth2/token`
- `get_market_price()` : `ka10001` 주식기본정보요청
- `get_account_balance()` : `kt00001` + `kt00018` 조합
- `place_order()` : `kt10000` 매수, `kt10001` 매도

## 5. 자동매매 흐름
1. `main.py` 가 설정을 읽는다.
2. `KiwoomBroker` 가 토큰을 발급받는다.
3. 계좌와 시세를 조회한다.
4. 전략이 BUY / SELL / HOLD 를 결정한다.
5. 리스크 관리가 주문 가능 여부를 검사한다.
6. 주문을 실행하고 로그를 남긴다.

## 6. 왜 계좌 조회를 두 API로 나누나?
키움 REST 응답 특성상
- `kt00001` 은 예수금/주문가능금액 쪽 확인에 유리했고
- `kt00018` 은 실제 보유 종목 목록 확인에 유리했습니다.

그래서 ver1 에서는 두 응답을 합쳐서 내부 `AccountBalance` 로 만들었습니다.

## 7. 자바 이식 포인트
파이썬에서 아래 구조를 그대로 유지하면 자바로 옮기기 쉽습니다.
- `MarketPrice` -> Java record / DTO
- `Position` -> Java record / DTO
- `Broker` -> Java interface
- `KiwoomBroker` -> Spring service
- `TradingService` -> application service

## 8. 남은 작업
- 체결조회 추가
- 장종료/휴장일 체크 고도화
- 연속조회 자동 반복 처리
- 자동매매 중복 주문 방지 강화
- 종목별 전략 파라미터 분리
