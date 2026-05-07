# Kiwoom Auto Trader

키움증권 REST API + WebSocket 기반 주식 자동매매 시스템

---

## 프로젝트 구조

```
kiwoom-auto-trader-ver1/
├── app/
│   └── main.py                    # 진입점 (REST + WebSocket 병렬 실행)
├── config/
│   ├── settings.py                # 설정 파싱
│   └── settings.yaml              # 전체 설정
├── domain/
│   ├── models.py                  # 도메인 모델
│   ├── market_regime/
│   │   └── classifier.py          # 장세 분류기
│   ├── risk/
│   │   └── risk_manager.py        # 리스크 관리
│   ├── service/
│   │   └── trading_service.py     # 매매 루프 핵심 서비스
│   └── strategy/
│       ├── base.py                # 전략 인터페이스
│       ├── breakout_strategy.py   # 3중 필터 매매 전략
│       ├── hold_strategy.py       # 횡보/하락장 관망 전략
│       └── strategy_router.py     # 장세별 전략 선택
├── infra/
│   ├── broker/
│   │   ├── base.py                # 브로커 인터페이스
│   │   ├── kiwoom_broker.py       # 키움 REST API 구현
│   │   └── mock_broker.py         # 테스트용 Mock 브로커
│   ├── storage/
│   │   ├── daily_reporter.py      # 일일 리포트 생성
│   │   ├── logger.py              # 앱/거래 로그
│   │   └── state_store.py         # 상태 저장 (JSON)
│   └── websocket/
│       ├── kiwoom_ws.py           # WebSocket 기본 클라이언트
│       ├── condition_watcher.py   # 조건검색 구독/편입/편출
│       └── real_token.py          # 실전 계좌 토큰 발급
├── utils/
│   └── time_utils.py              # 장 시간 유틸
├── logs/                          # 로그 및 리포트 저장
├── data/                          # 상태 파일 저장
├── .env                           # 환경변수 (앱키/시크릿키)
├── requirements.txt
└── README.md
```

---

## 환경 설정

### 1. 패키지 설치

```bash
pip install -r requirements.txt
```

### 2. `.env` 파일 생성

```env
# 모의투자 계좌 (주문용)
KIWOOM_APP_KEY=모의투자_앱키
KIWOOM_SECRET_KEY=모의투자_시크릿키
KIWOOM_ACCOUNT_NUMBER=모의투자_계좌번호

# 실전 계좌 (조건검색 WebSocket 전용)
KIWOOM_REAL_APP_KEY=실전_앱키
KIWOOM_REAL_SECRET_KEY=실전_시크릿키
```

### 3. 실행

```bash
python -m app.main
```

---

## 매매 전략

### 장세 분류 (1시간마다 갱신)

일봉 데이터 기반으로 4가지 지표를 조합해 장세를 분류합니다.

| 지표 | 내용 |
|---|---|
| 이동평균 | 5일선 vs 20일선 골든/데드크로스 |
| RSI | 수치 + 방향(↑상승 / ↓하락 / →보합) |
| MACD | 골든크로스 / 데드크로스 |
| 거래량 | 20일 평균 대비 1.5배 이상 급증 여부 |

| 장세 | 조건 | 전략 |
|---|---|---|
| BULLISH | MA 상승 + RSI 정상 + MACD 골든크로스 | 3중 필터 매수 전략 |
| SIDEWAYS | 위 조건 미충족 | 신규 매수 차단, 보유분만 관리 |
| BEARISH | MA 하락 + RSI 정상 + MACD 데드크로스 | 신규 매수 차단, 손절 우선 |
| UNKNOWN | 데이터 부족 | 보수적 관망 |

### 매수 조건 (BULLISH일 때, 3중 필터)

```
① RSI 30 이하 + 상승 중(↑)   → 바닥 확인
② MACD 골든크로스             → 상승 모멘텀 시작
③ 거래량 급증                 → 세력 유입 확인

3개 충족 → 강력 매수 (황금시간)
2개 충족 → 보수적 매수
1개 이하 → HOLD
```

### 매도 조건

```
익절: 평균단가 대비 +2.0%
손절: 평균단가 대비 -1.5%
조기 매도: RSI 70 이상 + MACD 데드크로스 (고점 반전 감지)
장 마감 10분 전: 전량 강제청산
```

### 리스크 관리

```
1회 주문 금액   : 20만원
최대 보유 종목  : 2개
최소 현금 유지  : 10만원
일일 최대 손실  : 10만원
재진입 쿨다운   : 매도 후 10분간 동일 종목 재매수 차단
```

---

## 조건검색 WebSocket 연동

HTS(영웅문)에서 만든 조건식을 실시간으로 구독합니다.

### 설정 방법

1. 영웅문 → 조건검색 → 조건식 생성 후 저장
2. `settings.yaml` 수정

```yaml
websocket:
  enabled: true
  url: wss://api.kiwoom.com:10000/api/dostk/websocket
  condition_seq: 0    # HTS에서 저장한 조건식 번호
  max_symbols: 5
```

### 동작 방식

```
조건 충족 종목 편입(I) → targets에 자동 추가
조건 이탈 종목 편출(D) → targets에서 자동 제거
```

> 모의투자 환경에서는 조건검색 WebSocket이 지원되지 않습니다.
> 조건검색은 실전 계좌로 연결하고, 주문은 모의 계좌로 실행합니다.

---

## 로그 예시

```
[REGIME] 001510 | BULLISH | MA 상승(5,326>4,274) + RSI 28.3↑ 정상 + MACD 골든크로스(+12.3) + 거래량급증
[BUY  ] [BULLISH] 001510 | 현재가 5,250원 | 황금시간 3중 조건 모두 충족 — RSI 28.3↑✓ | MACD 골든크로스✓ | 거래량 급증✓
[ORDER] 001510 | 매수 주문 접수 완료 | 수량 38주 | 주문번호 0081061
[SELL ] [BULLISH] 001510 | 현재가 5,355원 | 익절 목표 5,355원 도달
[ORDER] 001510 | 매도 주문 접수 완료 | 수량 38주 | 주문번호 0082145
[COOL ] 001510 | 매도 후 재진입 쿨다운 중 (599초 남음 / 총 600초)
[COND ] 편입: 005930 — targets에 추가됩니다
```

---

## 일일 리포트

장 마감 시 `logs/daily_report_날짜.txt` 자동 생성

```
=============================================
  일일 매매 리포트  2026-05-07
=============================================

[ 매매 요약 ]
  총 주문 수  : 5건
  매수        : 3건  (81주)
  매도        : 2건  (44주)
  체결 성공   : 4건 / 실패 : 1건

[ 종목별 내역 ]
  001510  매수 2회(75주) / 매도 1회(38주)
  047040  매수 1회(6주) / 매도 1회(6주)

[ 장세 판단 ]
  001510  SIDEWAYS (RSI 76.4↓ MACD +435.5)
  047040  SIDEWAYS (RSI 63.5↓ MACD +4276.9)

[ 참고 ]
  실제 손익은 증권사 앱에서 확인하세요.
=============================================
```

---

## 사용 API

| API | 설명 |
|---|---|
| `/oauth2/token` | 토큰 발급 |
| `ka10001` | 주식 현재가 조회 |
| `ka10081` | 주식 일봉 차트 조회 |
| `ka10171` | 조건검색 목록 조회 (WebSocket) |
| `ka10172` | 조건검색 일반 조회 (WebSocket) |
| `ka10173` | 조건검색 실시간 구독 (WebSocket) |
| `ka10174` | 조건검색 실시간 해제 (WebSocket) |

---
