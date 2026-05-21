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
│   │   ├── classifier.py          # 장세 분류기
│   │   └── minute_analyzer.py     # 분봉 분석기
│   ├── risk/
│   │   └── risk_manager.py        # 리스크 관리
│   ├── service/
│   │   └── trading_service.py     # 매매 루프 핵심 서비스
│   └── strategy/
│       ├── base.py                # 전략 인터페이스
│       ├── breakout_strategy.py   # BULLISH 추격 매수 전략
│       ├── neutral_strategy.py    # NEUTRAL 반등/눌림목 전략
│       ├── bottom_strategy.py     # REBOUND 바닥권 매수 전략
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
│   │   └── state_store.py         # 상태 저장 (JSON + 트레일링 최고가)
│   └── websocket/
│       ├── kiwoom_ws.py           # WebSocket 기본 클라이언트
│       ├── condition_watcher.py   # 조건검색 구독/편입/편출
│       └── real_token.py          # 실전 계좌 토큰 발급
├── utils/
│   └── time_utils.py              # 장 시간 유틸
├── logs/                          # 로그 및 리포트 저장
├── data/                          # 상태 파일 저장 (state.json)
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
| RSI | 수치 + 방향(↑상승 / ↓하락 / →보합) + Signal선(9일 EMA) |
| MACD | 골든크로스 / 데드크로스 / 히스토그램 방향 |
| 거래량 | 20일 평균 대비 1.5배 이상 급증 여부 |

| 장세 | 조건 | 전략 | 허용 매수 |
|---|---|---|---|
| BULLISH | MA↑ + RSI 정상 + MACD 골든크로스 | BreakoutStrategy | A돌파 + B반등 + C눌림목 |
| NEUTRAL | RSI 35~65 + 추세 불명확 | NeutralStrategy | B반등 + C눌림목만 |
| REBOUND | RSI≤35 + RSI Signal 골든 + MACD 히스토그램 반전 | BottomStrategy | 바닥권 안전 매수 |
| SIDEWAYS | 극단값 또는 과매수 구간 | HoldStrategy | 완전 관망 |
| BEARISH | MA↓ + RSI↓ + MACD 데드크로스 | HoldStrategy | 완전 관망 |
| UNKNOWN | 데이터 부족 | HoldStrategy | 보수적 관망 |

---

### BULLISH — BreakoutStrategy

분봉 2차 필터 + 6점 점수제 기반 단타 전략입니다.

**[1단계] 분봉 필터 — A/B/C 중 하나 통과**

| 조건 | 내용 | 눌림목 적용 |
|---|---|---|
| A 상승 돌파 | 당일 등락률 +2%~+18% | 고가 대비 -2% 이내 허용 |
| B 저점 반등 | 저점 대비 반등 +2% + VWAP 위 | 없음 |
| C 눌림목 | 등락률 -1%~-8% + MA5>MA20 + VWAP 위 | 고가 대비 -1%~-7% |

> A/B/C 조건별로 눌림목 기준이 다르게 적용됩니다.
> 기존에는 A조건 통과 후에도 눌림목을 강제하여 강한 종목을 차단하는 문제가 있었습니다.

**[2단계] 6점 점수제 — 2점 이상이면 매수**

```
① MACD 골든크로스
② MACD 모멘텀 가속 (히스토그램 확대)
③ 거래량 급증 (20일 평균 대비 1.5배)
④ 현재가 > MA5
⑤ 현재가 > VWAP
⑥ 분봉 저점 상승
```

---

### NEUTRAL — NeutralStrategy

추세가 애매한 구간에서 B/C 조건만 허용하는 전략입니다.

- B조건(저점 반등) 또는 C조건(눌림목) 중 하나 충족
- 점수제 **3점 이상** (BULLISH보다 엄격)
- A조건(상승 돌파) 불허 — 추세 불명확 구간의 고가 추격은 위험

---

### REBOUND — BottomStrategy

과매도 구간에서 반등 초입을 잡는 바닥권 매수 전략입니다.

**매수 조건 (필수 3가지 모두 충족)**

| 조건 | 내용 |
|---|---|
| ① RSI Signal 골든크로스 | RSI(14) < 35 + RSI가 Signal(9)을 상향 돌파 |
| ② MACD 히스토그램 반전 | 음수 구간에서 증가 시작 |
| ③ 거래량 시나리오 A 또는 B | A: 매물고갈(거래량 < 평균 70%) / B: 세력유입(거래량 > 평균 130%) |

---

### 매도 조건 (전 전략 공통)

```
① 손절        : 평균단가 -1.5% (최우선)
② 트레일링    : 최고가 대비 -2% (최소 +1% 이상 오른 후 작동)
③ 추세 꺾임   : RSI ≥ 70 + RSI 하락전환 + MACD 히스토그램 축소
④ 안전망      : 평균단가 +15% (급등 시 확보)
```

> 고정 익절이 없습니다. 트레일링 스탑이 추세를 끝까지 추적합니다.

---

### 리스크 관리

```
1회 주문 금액   : 50만원
최대 보유 종목  : 5개
최소 현금 유지  : 10만원
일일 최대 손실  : 10만원
재진입 쿨다운   : 매도 후 10분간 동일 종목 재매수 차단
강제청산        : 15:18 (단일가 매매 시작 2분 전)
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
  max_symbols: 10
```

### 동작 방식

```
조건 충족 종목 편입(I) → targets에 자동 추가
조건 이탈 종목 편출(D) → targets에서 자동 제거
```

> 모의투자 환경에서는 조건검색 WebSocket이 지원되지 않습니다.
> 조건검색은 실전 계좌로 연결하고, 주문은 모의 계좌로 실행합니다.

---

## 로그 태그 체계

| 태그 | 의미 |
|---|---|
| `[REGIME]` | 장세 판단 결과 |
| `[BUY]` | 매수 신호 발생 |
| `[SELL]` | 매도 신호 발생 |
| `[ORDER]` | 주문 접수 완료 |
| `[FAIL]` | 주문 실패 |
| `[HOLD_POS]` | 보유 중 (수익률 + 트레일링 진행상황) |
| `[NEAR_TP]` | 익절가 2% 이내 근접 경고 |
| `[NEAR_SL]` | 손절가 1% 이내 근접 경고 |
| `[BLOCK]` | 분봉 필터 차단 (사유 명시) |
| `[HOLD]` | 일반 홀딩 (장세 불일치) |
| `[MIN]` | 분봉 분석 결과 |
| `[COOL]` | 재진입 쿨다운 중 |
| `[EXCL]` | UNKNOWN 3회 → 자동 제외 |
| `[COND]` | 조건검색 편입/편출 |

### 로그 예시

```
[REGIME  ] 005930 | BULLISH | MA 상승(298,000>275,000) + RSI 62.1↑ + MACD 골든크로스(+45.2)
[MIN     ] 005930 | 4/5 | VWAP 위✓(297,759) | 저점 상승✓ | 눌림 불량✗(-0.2%) | A등락 유효✓(+8.3%) | 거래대금 충분✓(97046억)
[BUY     ] [BULLISH] 005930 | 현재가 298,000원 | 최적 타점 4/6 — MACD 골든✓ | 거래량 급증✓ | VWAP 위✓ | 저점 상승✓
[ORDER   ] 005930 | 매수 주문 접수 완료 | 수량 1주 | 주문번호 0081061
[HOLD_POS] 005930 | 현재가 302,000원 (+1.3%) | 트레일링 시작까지 +1% 필요 / 손절 293,530원
[HOLD_POS] 005930 | 트레일링 추적 중 — 최고가 310,000원 / 스탑 303,800원 / 현재 +4.0%
[SELL    ] 005930 | 트레일링 스탑 — 최고가 310,000원 대비 -2.1% 하락 (보유 수익 +3.8%)
[COOL    ] 005930 | 매도 후 재진입 쿨다운 중 (598초 남음 / 총 600초)
```

---

## 일일 리포트

장 마감 시 `logs/daily_report_날짜.txt` 자동 생성

```
══════════════════════════════════════════════════
  📊 일일 매매 리포트  2026-05-08 (목)
══════════════════════════════════════════════════

[ 💰 손익 요약 ]
  실현 손익   :    +7,650원
  매수 총액   :   236,500원  (2건)
  매도 총액   :   244,150원  (2건)
  승률        : 1승 1패 (50%)

[ 📋 종목별 상세 ]
  010170  매수 21,500원 x9주  →  매도 22,350원  +7,650원 (+4.0%)  ✅
  000270  매수 166,800원 x1주 →  매도 164,000원  -2,800원 (-1.7%)  ❌

[ 📈 매매 통계 ]
  총 주문     : 6건  (성공 4 / 실패 2)
  매수        : 2건  (10주)
  매도        : 2건  (10주)
  평균 보유   : 약 137분

[ ℹ️  참고 ]
  표시 손익은 주문가 기준 예상값입니다. 실제 체결가와 다를 수 있습니다.
  정확한 손익은 증권사 앱에서 확인하세요.
══════════════════════════════════════════════════
```

---

## 주요 설정값 (`settings.yaml`)

```yaml
targets:
  symbols:
    - "001510"
    - "047040"

trading:
  poll_interval_seconds: 10          # 루프 주기
  order_cash_per_trade: 500000       # 1회 주문 금액
  max_positions: 5                   # 최대 보유 종목 수
  force_exit_before_market_close_minutes: 12  # 15:18 강제청산
  reentry_cooldown_seconds: 600      # 재진입 쿨다운 (10분)
  price_refresh_seconds: 60          # 가격 캐시 주기

strategy:
  stop_loss_pct: 1.5                 # 손절 기준
  trailing_stop_pct: 2.0             # 트레일링 스탑
  trailing_start_pct: 1.0            # 트레일링 시작 최소 수익률
  take_profit_pct: 15.0              # 안전망 익절
  trend_reversal_rsi: 70.0           # 추세 꺾임 RSI 기준

market_regime:
  rsi_overbought: 80.0
  rsi_oversold: 20.0
  min_trading_value: 1000000000      # 거래대금 최소 10억
  pullback_min_pct: -7.0             # 눌림목 하한
  pullback_max_pct: -1.0             # 눌림목 상한
  change_rate_min: 2.0               # A조건 등락률 하한
  change_rate_max: 18.0              # A조건 등락률 상한
  rebound_min_pct: 2.0               # B조건 반등률 최소

websocket:
  enabled: true
  condition_seq: 0
  max_symbols: 10
```

---

## 사용 API

| API | 설명 |
|---|---|
| `/oauth2/token` | 토큰 발급 |
| `ka10001` | 주식 현재가 조회 |
| `ka10081` | 주식 일봉 차트 조회 |
| `ka10080` | 주식 분봉 차트 조회 |
| `kt10001` | 주식 매수/매도 주문 |
| `ka10171` | 조건검색 목록 조회 (WebSocket) |
| `ka10172` | 조건검색 일반 조회 (WebSocket) |
| `ka10173` | 조건검색 실시간 구독 (WebSocket) |
| `ka10174` | 조건검색 실시간 해제 (WebSocket) |

---

## 버전 히스토리

| 버전 | 주요 변경 |
|---|---|
| v1.1 | 키움 모의투자 REST API 연동, 기본 자동매매 루프 |
| v1.2 | 장세 분류기, 3중 필터 매수, 핑퐁 차단, WebSocket 조건검색 |
| v1.3 | 트레일링 스탑, NEUTRAL/REBOUND 장세, 바닥권 매수, 매수 모드 분리, 단일 전략 통합 |
