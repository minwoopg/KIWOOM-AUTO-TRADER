# Kiwoom Auto Trader ver1

파이썬 초보자도 따라가기 쉽도록 **설명형 주석과 docstring**을 많이 넣은 버전입니다.

## 이 버전에서 달라진 점
이제 `KiwoomBroker`가 단순 스켈레톤이 아니라, 실제로 아래 API들을 호출하도록 반영되어 있습니다.

- 토큰 발급
- 현재가 조회
- 예수금 조회
- 보유 종목 조회
- 매수 주문
- 매도 주문

즉, 사용자가 직접 검증한 키움 REST 모의투자 흐름을 프로젝트 구조 안으로 옮긴 버전입니다.

## 현재 구현 범위
- 키움증권 **모의투자 REST API 연동**
- 자바 이식 전제의 레이어 분리 구조
- 간단한 돌파 전략 1개 포함
- 상태 저장(JSON), 거래 로그(CSV), 앱 로그(file) 포함
- MockBroker / KiwoomBroker 둘 다 제공

## 브로커 선택 방식
### 1) 공부용 가짜 브로커
`config/settings.yaml` 에서 아래처럼 두면 키움 API를 전혀 호출하지 않습니다.

```yaml
broker:
  use_mock: true
```

### 2) 키움 모의투자 실연동
아래처럼 두면 실제 키움 모의투자 REST API를 호출합니다.

```yaml
broker:
  use_mock: false
  base_url: https://mockapi.kiwoom.com
```

### 3) 키움 실전투자
실전으로 바꿀 때는 URL 을 바꾸고, 앱키/시크릿키도 실전용으로 교체해야 합니다.

```yaml
broker:
  use_mock: false
  base_url: https://api.kiwoom.com
```

## 중요한 보안 원칙
- 앱키 / 시크릿키 / 토큰은 채팅창에 붙여넣지 마세요.
- `.env` 파일에 넣고 코드에서는 환경변수로만 읽는 편이 안전합니다.
- 실전투자는 반드시 모의투자를 충분히 검증한 뒤에만 진행하세요.

## .env 파일 예시
프로젝트 루트에 `.env` 파일을 만들고 아래처럼 입력합니다.

```env
KIWOOM_APP_KEY=여기에_앱키
KIWOOM_SECRET_KEY=여기에_시크릿키
KIWOOM_ACCOUNT_NUMBER=여기에_계좌번호
```

## 처음 읽는 순서 추천
1. `docs/detailed_design.md`
2. `config/settings.yaml`
3. `app/main.py`
4. `infra/broker/kiwoom_broker.py`
5. `domain/service/trading_service.py`
6. `domain/strategy/breakout_strategy.py`

## 실행 방법
### 1. 패키지 설치
```bash
python -m pip install -r requirements.txt
```

### 2. 환경변수 파일 준비
프로젝트 루트에 `.env` 파일 생성

### 3. 실행
```bash
python app/main.py
```

## 지금 구조에서 자동매매가 돌아가는 원리
1. `main.py` 가 설정과 브로커를 준비합니다.
2. `KiwoomBroker.authenticate()` 가 토큰을 발급받습니다.
3. `TradingService.run_once()` 가 계좌/시세를 조회합니다.
4. `BreakoutStrategy` 가 BUY / SELL / HOLD 를 판단합니다.
5. `RiskManager` 가 주문 가능 여부를 검사합니다.
6. 통과하면 `KiwoomBroker.place_order()` 가 실제 주문을 전송합니다.
7. 결과는 CSV 와 로그 파일에 남습니다.

## 아직 남은 보강 포인트
- 장 상태를 더 정교하게 체크하기
- 주문 후 체결 조회 로직 추가
- 연속조회(next-key) 자동 처리
- 종목별 수수료/세금 반영 고도화
- 손익 집계 자동화

## 주의
현재 `main.py` 는 매우 단순한 첫 버전입니다.
즉, 장중에 조건이 맞으면 바로 주문을 시도합니다.
실제로 굴리기 전에는 반드시 종목 수, 주문 금액, 전략 조건을 아주 보수적으로 두세요.


## ver1.1 변경사항 요약
- 키움증권 REST API 모의투자 기준으로 자동매매 프로젝트 구조를 정비하였습니다.
- 토큰 발급, 계좌 조회, 현재가 조회, 예수금 조회, 보유종목 조회를 실제로 검증하였습니다.
- 모의 매수 주문과 매도 주문 API 호출을 성공적으로 테스트하였습니다.
- 실행 방식을 python -m app.main 기준으로 통일하여 import 문제를 해결하였습니다.
- .env 기반 설정 로드를 정리하여 앱키, 시크릿키, 계좌번호를 외부에서 관리하도록 변경하였습니다.
- 동일 종목 재진입 허용 여부를 설정으로 제어할 수 있도록 정비하였습니다.
- 주문 성공 후 잔고 캐시를 무효화하도록 수정하여 매도 후 재진입 판단 오류를 줄였습니다.
- 계좌 조회와 현재가 조회에 캐시 구조를 도입하여 API 호출 빈도를 낮추도록 개선하였습니다.
- 429 요청 제한 발생 시 백오프 로직을 추가하여 반복 오류를 완화하였습니다.
- 전략 판단, 주문 성공/실패, 홀딩 사유를 사람이 읽기 쉬운 로그 형태로 개선하였습니다.