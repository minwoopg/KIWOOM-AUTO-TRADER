# Kiwoom Auto Trader

키움증권 REST API + WebSocket 기반 주식 자동매매 시스템

---

## 프로젝트 구조

```
kiwoom-auto-trader-ver1/
├── app/
│   ├── main.py                    # 진입점 (REST + WebSocket 병렬 실행)
│   └── target_selection.py        # 조건검색 편입 종목 → 당일 감시 목록 계산
├── config/
│   ├── settings.py                # 설정 파싱
│   └── settings.yaml              # 전체 설정
├── domain/
│   ├── models.py                  # 도메인 모델
│   ├── cost_model.py              # 거래 비용 모델 (Gross/Base/Stress 3중 추정, 단일 출처)
│   ├── replay_context.py          # 리플레이 시간축 공용 컨텍스트
│   ├── shadow_signature.py        # shadow 로그 중복판정 공용 서명 함수
│   ├── indicator/
│   │   └── indicators.py          # RSI/MACD/이동평균 등 지표 계산
│   ├── market_regime/
│   │   ├── classifier.py          # 장세 분류기
│   │   ├── minute_analyzer.py     # 분봉 분석기 (A/B/C/D/V/PR 패턴 판정)
│   │   └── session_metrics.py     # 당일 세션 기준 지표 (shadow, 60분 롤링과 병행 관측)
│   ├── position/
│   │   └── lifecycle.py           # 포지션 상태 머신 (PSM) — 체결 확인 게이트
│   ├── risk/
│   │   └── risk_manager.py        # 리스크 관리
│   ├── service/
│   │   ├── trading_service.py     # 매매 루프 핵심 서비스
│   │   └── pnl_calculator.py      # FIFO 방식 손익 계산 (공통 모듈)
│   └── strategy/
│       ├── base.py                # 전략 인터페이스
│       ├── breakout_strategy.py   # BULLISH 추격 매수 전략
│       ├── neutral_strategy.py    # NEUTRAL 반등/눌림목 전략
│       ├── bottom_strategy.py     # REBOUND 바닥권 매수 전략
│       ├── hold_strategy.py       # 횡보/하락장 관망 전략
│       ├── strategy_router.py     # 장세별 전략 선택
│       ├── candidate_a_guard.py   # 진입 차단 게이트 — 상승여력 낮음+반등spike 없음 (현재 enforce)
│       └── entry_quality_shadow.py  # VWAP 거리 기반 진입 품질 shadow 관측
├── infra/
│   ├── broker/
│   │   ├── base.py                # 브로커 인터페이스
│   │   ├── kiwoom_broker.py       # 키움 REST API 구현
│   │   ├── kiwoom_parsing.py      # 응답 숫자 파싱 공용 helper
│   │   ├── kiwoom_order_status.py # 미체결/체결 조회 응답 → BrokerOrder 변환 (실측 기반, 불확실 시 fail-close)
│   │   ├── minute_bar_diagnostics.py  # 분봉 API 원본 응답 진단
│   │   └── mock_broker.py         # 테스트용 Mock 브로커
│   ├── notify/
│   │   └── kakao_notifier.py      # 카카오톡 매수/매도/시작 알림
│   ├── storage/
│   │   ├── daily_reporter.py      # 일일 리포트 생성
│   │   ├── logger.py              # 앱/거래/관측 로그
│   │   ├── state_store.py         # 상태 저장 (JSON + 트레일링 최고가)
│   │   ├── state_reconciler.py    # 시작 시 state.json ↔ 실제 잔고 동기화
│   │   ├── tracked_order_journal.py  # 체결 확정 전 주문의 사실을 디스크에 원자적으로 보존 (재시작 복원용)
│   │   ├── process_lock.py        # 프로세스 단일 인스턴스 락
│   │   ├── run_baseline.py        # 실행마다 git sha/설정 해시 등 실행 기준선 기록
│   │   ├── skip_reason.py         # signal_log.csv용 SKIP 사유 표준 상수
│   │   └── minute_bar_saver.py    # 1분봉 원본 저장 (리플레이/검증용)
│   └── websocket/
│       ├── kiwoom_ws.py           # WebSocket 기본 클라이언트
│       ├── condition_watcher.py   # 조건검색 구독/편입/편출
│       └── real_token.py          # 실전 계좌 토큰 발급
├── utils/
│   ├── time_utils.py              # 장 시간 유틸
│   └── trade_outcome.py           # 거래 결과 분류 공용 함수
├── tools/                          # 감사/사후분석용 스크립트 (audit_trade_bundles, profitability_sprint 등)
├── analyze_*.py, validate_*.py, replay_runner.py 등  # 루트의 오프라인 분석·리플레이·검증 스크립트 모음
│                                                       # (실거래 루프와 무관, 사후 데이터 분석 전용)
├── run_regression_tests.py        # 전체 회귀 테스트 러너
├── logs/                          # 로그 및 리포트 저장
├── data/                          # 상태 파일 저장 (state.json, minute_bars/, tracked_order_journal.json)
├── .env                           # 환경변수 (앱키/시크릿키)
├── requirements.txt
├── CHANGELOG_v1.x.md              # 버전별 상세 변경 이력
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

# 카카오톡 알림 (선택 — 없으면 알림 없이 동작)
KAKAO_ACCESS_TOKEN=...
KAKAO_REFRESH_TOKEN=...
KAKAO_REST_API_KEY=...
```

### 3. 실행

```bash
python -m app.main
```

시작 시 순서: `.env`/설정 로드 → 브로커 인증 → **실제 잔고와 `state.json` 동기화**(실패 시 실전투자는 시작 자체를 중단, 모의투자는 경고만) → 실행 기준선 기록(run_baseline) → 카카오 시작 알림(비동기, 실패해도 매매 시작을 막지 않음) → 조건검색 WebSocket 활성화 시 REST 매매 루프와 병렬 실행.

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
| BULLISH | MA↑ + RSI 정상 + MACD 골든크로스 | BreakoutStrategy | A돌파 + C눌림목 + D갭눌림목 (+V자/PR 결합) |
| NEUTRAL | RSI 35~65 + 추세 불명확 | NeutralStrategy | C눌림목 + V자/PR 필수 결합만 |
| REBOUND | RSI≤35 + RSI Signal 골든 + MACD 히스토그램 반전 | BottomStrategy | 바닥권 안전 매수 |
| SIDEWAYS | 극단값 또는 과매수 구간 | HoldStrategy | 완전 관망 |
| BEARISH | MA↓ + RSI↓ + MACD 데드크로스 | HoldStrategy | 완전 관망 |
| UNKNOWN | 데이터 부족 | HoldStrategy | 보수적 관망 |

> **B조건(저점 반등) 단독 매수는 현재 BULLISH·NEUTRAL 모두 비활성화되어 있습니다** —
> 반복 손실로 2026-07에 끈 뒤 재활성화하지 않은 상태입니다. VWAP/V자/PR
> 판정 내부 계산에는 여전히 쓰이지만, "B 단독"으로는 매수하지 않습니다.

---

### BULLISH — BreakoutStrategy

분봉 2차 필터 → 8점 점수제 기반 단타 전략입니다.

**[1단계] 분봉 필터 — A/C/D 중 하나(또는 V/PR) 통과**

| 조건 | 내용 | 눌림목 적용 |
|---|---|---|
| A 상승 돌파 | 당일 등락률 +2%~+18% | 고가 대비 -2% 이내 허용 |
| C 눌림목 | 당일 등락률 -17.5%~-0.3% + MA5>MA20 + VWAP 위 | 고가 대비 -1%~-7% 유지 |
| D 갭 눌림목 | 당일 +5%~+10% 급등 + VWAP 위 + 고가 대비 -0.1%~-3% 눌림 | 점수 2점만 넘으면 매수 허용(아래 참고) |
| V/PR | V자 반등 또는 눌림목 재상승 — A/C와 무관하게 단독으로도 필터 통과 인정 | — |

> C조건 범위는 2026-07 리플레이 검증(34거래일) 근거로 -7%~-1%에서
> -17.5%~-0.3%로 크게 완화된 상태입니다. `slow_v`(완만한 V자 반등, 9~120분
> 창)는 감지는 계속되지만 34일 백테스트 결과가 전 구간 마이너스라 실거래
> 반영은 꺼져 있습니다(`slow_v_enabled: false`).

**[2단계] 8점 점수제**

```
① MACD 골든크로스          ⑤ VWAP 위에 있는가
② MACD 모멘텀 가속         ⑥ 분봉 저점 상승
③ 거래량 급증 (20일 평균×1.5) ⑦ V자반등 또는 눌림목재상승(PR)
④ 현재가 > MA5             ⑧ 반등거래량 spike
```

기본 문턱은 **3점 이상 매수**이지만, `disable_score3_buy=true`라 **3점은
현재 매수하지 않고 사실상 4점 이상**만 매수합니다. D패턴(갭눌림목)은
예외적으로 **2점만 넘으면** 매수를 허용합니다.

다음 상황 중 하나라도 해당하면 문턱이 **5점**으로 상향됩니다(4점 강한
진입도 차단):
- 당일 등락률 +3% 이상인데 MACD가 데드 상태(추격매수 과열)
- 볼린저 %B ≥ 1.0 (상단 돌파 추격)
- 상승여력(최근 고점까지 여유) < 1.0% (단, D패턴은 이 게이트 예외)
- 종목별 개별 문턱(`symbol_min_score_override`, 현재 000660=5점 지정)

5점 진입은 추가로 **확인지표(거래량급증/V자/PR/반등spike) 중 최소
1개**가 없으면 통과시키지 않습니다(동행지표만으로 채워진 5점의 실거래
성과가 뚜렷이 열세였던 근거).

---

### NEUTRAL — NeutralStrategy

추세가 애매한 구간에서 C(눌림목) 매수만 허용하고, **V자 반등 또는
PR(눌림목 재상승) 중 하나가 반드시 함께 확인되어야** 합니다(C 단독
불허). A(상승 돌파)는 추세 불명확 구간의 고가 추격 위험 때문에 아예
제외됩니다.

- 점수제 **5점 이상**(BULLISH보다 훨씬 엄격 — 8점 만점 기준)
- BULLISH의 추격매수/볼린저/상승여력 게이트는 적용되지 않음(애초에
  5점 문턱 자체가 그 역할을 겸함)

---

### REBOUND — BottomStrategy

과매도 구간에서 반등 초입을 잡는 바닥권 매수 전략입니다.

**매수 조건 (필수 3가지 모두 충족)**

| 조건 | 내용 |
|---|---|
| ① RSI Signal 골든크로스 | RSI(14) < 35 + RSI가 Signal(9)을 상향 돌파 |
| ② MACD 히스토그램 반전 | 음수 구간에서 증가 시작 |
| ③ 거래량 시나리오 A 또는 B | A: 매물고갈(거래량 < 평균 70%) / B: 세력유입(거래량 > 평균 130%) |

**매도**: 손절 -1.5% → 트레일링(+3% 이상부터 시작, 최고가 대비 -2%
고정폭) → 안전망 +15% — 3전략 중 유일하게 구간형이 아닌 고정폭
트레일링을 그대로 사용합니다.

---

### 매도 조건

**3전략(BULLISH/NEUTRAL/REBOUND) 모두 다음 순서로 판단하지만, 트레일링
시작점·폭은 전략마다 다르게 튜닝되어 있습니다.**

```
① 손절        : 평균단가 -1.5% (전 전략 공통, 최우선)
② 구간형 트레일링 (전략별 상이 — 아래 표)
③ 추세 꺾임   : 5점제, 보유수익 +0.5% 이상일 때만 작동 (전 전략 공통 기준)
④ 안전망      : 평균단가 +15% (전 전략 공통)
```

| 전략 | 트레일링 시작 | 폭 (수익 구간별) |
|---|---|---|
| BULLISH | 최고가 기준 +1.2% 이상 | +5.0%↑→2.8% / +3.5%↑→2.2% / +2.0%↑→1.8% / 그 외→1.5% |
| NEUTRAL | 최고가 기준 +0.5% 이상 | +3.0%↑→2.0% / +2.0%↑→1.5% / +1.0%↑→1.2% / 그 외→0.8% |
| REBOUND | 최고가 기준 +3.0% 이상 | 고정 2.0% |

**③ 추세 꺾임**은 다음 5개 중 **3점 이상 + 실제로 VWAP 또는 MA5
이탈**이 함께 확인되어야 SELL이 나갑니다(점수만 3점이고 가격이 아직
VWAP/MA5 위면 SELL 아님) — 지표 기반 판단이라 `requires_fresh_minute_
data=True`로 신선한 분봉이 없으면 이 SELL은 보류됩니다.

```
RSI ≥ trend_reversal_rsi(70) / RSI 하락전환 / MACD 히스토그램 축소 /
VWAP 이탈 / MA5 이탈
```

> 고정 익절이 없습니다. 트레일링 스탑이 추세를 끝까지 추적합니다.
> `settings.yaml`의 `trailing_stop_pct`(2.0%)는 현재 **REBOUND(BottomStrategy)
> 매도에만 실제로 쓰이고**, BULLISH/NEUTRAL은 위 표의 하드코딩된 구간형
> 값을 사용합니다 — 설정을 바꿔도 BULLISH/NEUTRAL 트레일링 폭은 바뀌지
> 않으니 주의하세요.

---

### entry_watch — 매수 후 초반 관찰 (정규 전략보다 먼저 실행)

매수 후 `watch_minutes`(5분) 동안은 위 정규 전략의 SELL 판단보다
**먼저** 다음을 확인합니다. 여기서 SELL이 나가면 그 폴링에서는 정규
전략 SELL 로직을 아예 호출하지 않습니다.

```
1) 급락 즉시청산   : 수익률 ≤ -1.0% (fail_cut_pct)
2) VWAP 이탈청산   : 매수 30초 유예 + 0.2%p 이상 이탈 + 2회 연속 확인
                     (히스테리시스 — 노이즈로 인한 조기 청산 방지)
3) 최소수익 미달청산: watch_minutes(5분) 경과 시점에 수익률 < 0.5%면 청산
```

세 조건 모두 해당 없으면 `None`을 반환해 정규 전략(손절/트레일링/추세꺾임)
에 판단을 그대로 위임합니다. 이 관찰 로직 자체는 **관측 목적의 부가
기록**(어떤 진입 건이 5분 시점에 실제로 평가됐는지)도 함께 남기지만,
그 기록 실패가 SELL 판단에 영향을 주지 않도록 예외를 전부 격리해
두었습니다(`logs/delayed_eval_candidate.csv` — 순수 관측용).

---

### Candidate A 진입 차단 게이트 (현재 enforce, 실제 주문 차단 중)

`upside_to_recent_high_pct < 0.50%`(최근 고점까지 상승여력이 거의 없음)
**AND** `rebound_volume_spike`가 없음(반등 거래량 확인 안 됨) 조건이
동시에 성립하면, 위 전략들이 BUY를 내더라도 **실제 주문 제출 자체를
차단**합니다(`domain/strategy/candidate_a_guard.py`).

- shadow 관측(2026-08-26~) 100% skip precision·0% 승자 손상 확인 후
  2026-09-03 민우님 승인으로 enforce 전환.
- enforce 전환 이후 최소 3~5 clean 거래일 동안 이 게이트 외 다른 매매
  로직(Candidate G/M1/MIN_PROFIT_5M/CRASH_CUT/entry score/BULLISH 등)은
  전부 동결하기로 결정된 상태 — 성과 귀속을 이 게이트 하나로 한정하기
  위함.
- 졸업/재심사 기준: 실제 차단(actual blocks) ≥10건, 종목 ≥5개, 독립
  클러스터 ≥3개, clean day ≥3일 — 충족 시 permanent adoption 여부를
  별도 재검토합니다.

---

### 리스크 관리

```
1회 주문 금액   : 600만원
최대 보유 종목  : 5개
최소 현금 유지  : 10만원
일일 최대 손실  : 100만원
연속 손절 허용  : 3회 (종목별 첫 손실만 전역 카운터에 반영 — 한 종목의
                  불운이 계좌 전체 매수를 막지 않도록 2026-06-15 결정)
재진입 쿨다운   : 매도 후 10분간 동일 종목 재매수 차단
종목당 1일 진입 : 원칙적으로 1회(allow_multiple_entries_per_symbol_per_day=false),
                  안전망으로 최대 3회 상한도 별도 유지
트레일링 손실   : 1회만 나도 해당 종목 60분 쿨다운
강제청산        : 15:10 (단일가 매매 시작 20분 전, 수익쿠션 +0.3% 미만 이월분만)
영구 제외 종목  : 인버스 ETF 2종 + 과거 반복 손실/고위험 이력 종목 다수
                  (settings.yaml `excluded_symbols` 참고)
```

---

## 체결 안전성 & 운영 인프라

실거래 운영 중 겪은 사고들을 계기로, 신호 판단 로직과는 별도로 "실제
주문이 진짜 어떻게 됐는지"를 확정하는 안전장치들이 추가되어 있습니다.

- **포지션 상태 머신(PSM)** — `domain/position/lifecycle.py`. `BUY_PENDING
  → OPEN → SELL_PENDING → FLAT`(+`ERROR`/orphan) 5단계로 관리하며, 실제
  BUY/SELL 주문 제출 자체를 게이트합니다(이름은 shadow였던 시절 그대로지만
  현재는 실동작에 관여). 잔고와 상태가 안 맞으면 `[POSITION_STATE_MISMATCH]`
  로 즉시 드러내고, 사람이 파일 명령(`commands/ack_error_{symbol}.json`
  등)으로만 수동 해제할 수 있습니다.
- **Tracked Order Journal** — 체결이 아직 확정 안 된 주문의 `order_id`
  사실을 디스크에 원자적으로 남겨, 프로세스가 재시작돼도 "이 종목에
  미해결 주문이 있었다"는 사실 자체가 사라지지 않게 합니다.
- **StateReconciler** — 시작 시 `state.json`과 실제 브로커 잔고를 대조해
  동기화합니다. **실전투자는 이 동기화가 실패하면 시작 자체를 중단**하고
  (실제 보유와 로컬 상태가 다른 채로 매매를 시작하지 않기 위함), 모의투자는
  경고만 남기고 진행합니다.
- **프로세스 단일 인스턴스 락** — 같은 `state.json`으로 두 프로세스가 동시에
  뜨는 것을 OS 파일 락으로 방지합니다.
- **잔고 조회 캐시 정책** — 기본은 180초(`balance_refresh_seconds`) 캐시
  재사용이지만, **미해결 주문이 있으면 매 폴링마다 강제로 새로 조회**합니다.
  조회가 429 등으로 실패하면 **과거 캐시로 대체하지 않고 예외를 그대로
  전파**합니다(스테일 수량을 "이번 체결 확인 결과"로 오인하는 사고를
  막기 위한 2026-09-14 안전 복구 — 대신 이 실패가 그 폴링 사이클 전체를
  지연시킬 수 있다는 트레이드오프가 있고, 이 부분(180초 감시 공백)은
  현재 재설계가 진행 중입니다).
- **Run Baseline** — 매 실행마다 git commit SHA·dirty 여부·설정 해시·
  모의/실전 구분을 `logs/run_baseline.csv`에 한 줄 기록해, 나중에 "이
  거래가 어떤 코드/설정으로 나온 것인지" 역추적할 수 있게 합니다.

---

## 관측 전용 로그 (매매 판단에 영향 없음)

아래 로그들은 전부 **읽기 전용 관측 목적**입니다 — 기록이 실패해도
매수/매도/보유 판단에는 영향을 주지 않도록 예외를 격리해 두었습니다.

| 파일 | 내용 |
|---|---|
| `logs/balance_freshness.csv` | 잔고 조회가 캐시/신규조회/실패 중 어느 경로를 탔는지 매번 기록 |
| `logs/delayed_eval_candidate.csv` | entry_watch 5분 판정 시점 근방에서 유효 평가를 못 받은 진입 건 기록 |
| `logs/low_upside_shadow.csv` | Candidate A 조건 매치 여부와 실제 차단 결과 shadow 기록 |
| `logs/min_profit_extension_shadow.csv` | 최소수익 조건 연장 관련 shadow 기록 |
| `logs/run_baseline.csv` | 실행별 git sha/설정 해시 기준선 |

---

## 조건검색 WebSocket 연동

HTS(영웅문)에서 만든 조건식을 실시간으로 구독합니다. **여러 조건식을
동시에 구독**할 수 있습니다(현재 3개).

### 설정 방법

1. 영웅문 → 조건검색 → 조건식 생성 후 저장
2. `settings.yaml` 수정

```yaml
websocket:
  enabled: true
  url: wss://api.kiwoom.com:10000/api/dostk/websocket
  condition_seqs: [1, 2, 3]   # HTS에서 저장한 조건식 번호들 (동시 구독)
  max_symbols: 10
```

### 동작 방식

```
조건 충족 종목 편입(I) → targets에 자동 추가
조건 이탈 종목 편출(D) → targets에서 자동 제거 (단, 보유 중이면 모니터링 유지)
```

편출됐더라도 실제 보유 중인 종목은 손절/트레일링 감시에서 빠지지
않도록 별도로 계속 추적합니다.

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
| `[BLOCK]` | 분봉 필터 차단 (사유 명시) |
| `[HOLD]` | 일반 홀딩 (장세 불일치) |
| `[MIN]` | 분봉 분석 결과 |
| `[COOL]` | 재진입 쿨다운 중 |
| `[EXCL]` | UNKNOWN 3회 → 자동 제외 |
| `[COND]` / `[COND_STATUS]` | 조건검색 편입/편출 현황 |
| `[RECONCILE]` | 시작 시 state.json ↔ 실제 잔고 동기화 결과 |
| `[LIFECYCLE_STUCK]` | 포지션 상태머신이 오래 막혀 있음(사람 확인 필요) |
| `[LIFECYCLE_ORPHAN]` | 추적 못 한 주문이 잔고 변화로 해소됨 |
| `[POSITION_STATE_MISMATCH]` | 상태머신-잔고 불변조건 위반 감지 |

### 로그 예시

```
[REGIME  ] 005930 | BULLISH | MA 상승(298,000>275,000) + RSI 62.1↑ + MACD 골든크로스(+45.2)
[MIN     ] 005930 | 4/8 | VWAP 위✓(297,759) | 저점 상승✓ | 눌림 불량✗(-0.2%) | A등락 유효✓(+8.3%) | 거래대금 충분✓(97046억)
[BUY     ] [BULLISH] 005930 | 현재가 298,000원 | 강한 진입 4/8 — MACD 골든✓ | 거래량 급증✓ | VWAP 위✓ | 저점 상승✓
[ORDER   ] 005930 | 매수 주문 접수 완료 | 수량 1주 | 주문번호 0081061
[HOLD_POS] 005930 | 현재가 302,000원 (+1.3%) | 트레일링 시작까지 +1.2% 필요 / 손절 293,530원
[HOLD_POS] 005930 | 트레일링 추적 중 — 최고가 310,000원 / 스탑 303,800원 (폭 -1.8%) / 현재 +4.0%
[SELL    ] 005930 | 트레일링 스탑 — 최고가 310,000원 대비 -2.1% 하락 (트레일링 폭 -1.8% / 보유 수익 +3.8%)
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
    - "010170"
    - "006260"

trading:
  poll_interval_seconds: 10          # 루프 주기
  balance_refresh_seconds: 180       # 잔고 캐시 재사용 주기 (미해결 주문 있으면 매번 강제조회)
  price_refresh_seconds: 60          # 가격 캐시 주기
  order_cash_per_trade: 6000000      # 1회 주문 금액
  max_positions: 5                   # 최대 보유 종목 수
  force_exit_before_market_close_minutes: 20  # 15:10 강제청산 검토 시작
  force_exit_cushion_pct: 0.3        # 이 이상 수익이면 이월 허용
  reentry_cooldown_seconds: 600      # 재진입 쿨다운 (10분)
  allow_multiple_entries_per_symbol_per_day: false  # 종목당 1일 1회

strategy:
  stop_loss_pct: 1.5                 # 손절 기준 (전 전략 공통)
  trailing_stop_pct: 2.0             # REBOUND(BottomStrategy) 전용 — BULLISH/NEUTRAL은 하드코딩된 구간형 사용
  take_profit_pct: 15.0              # 안전망 익절
  trend_reversal_rsi: 70.0           # 추세 꺾임 RSI 기준
  disable_score3_buy: true           # BULLISH 3점 매수 비활성
  low_upside_guard_enabled: true     # 상승여력<1% 진입 문턱 상향

market_regime:
  pullback_min_pct: -17.5            # C조건(눌림목) 하한
  pullback_max_pct: -0.3             # C조건(눌림목) 상한
  change_rate_min: 2.0               # A조건 등락률 하한
  change_rate_max: 18.0              # A조건 등락률 상한
  min_trading_value: 50000000000     # 거래대금 최소 500억

entry_watch:
  enabled: true
  watch_minutes: 5                   # 매수 후 관찰 시간(분)
  min_profit_pct: 0.5                # 관찰 종료 시점 최소 수익률
  fail_cut_pct: -1.0                 # 급락 즉시청산 기준

risk:
  max_daily_loss_amount: 1000000     # 일일 손실 한도 (원)
  max_consecutive_losses: 3

websocket:
  enabled: true
  condition_seqs: [1, 2, 3]
  max_symbols: 10

experimental:
  candidate_a_guard_mode: "enforce"  # 현재 실제 주문 차단 중
  # 나머지 flag(decision_engine/position_lifecycle/reward_risk_guard/
  # candidate_ranking/trailing_breakeven)는 여전히 off
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
| `ka10075` / `ka10076` | 미체결/체결 조회 (재시작 시 주문 상태 복원) |
| `ka10171` | 조건검색 목록 조회 (WebSocket) |
| `ka10172` | 조건검색 일반 조회 (WebSocket) |
| `ka10173` | 조건검색 실시간 구독 (WebSocket) |
| `ka10174` | 조건검색 실시간 해제 (WebSocket) |

---

## 버전 히스토리

상세 변경 내역은 각 버전의 `CHANGELOG_v1.x.md`를 참고하세요.

| 버전 | 주요 변경 |
|---|---|
| v1.1 | 키움 모의투자 REST API 연동, 기본 자동매매 루프 |
| v1.2 | 장세 분류기, 3중 필터 매수, 핑퐁 차단, WebSocket 조건검색 |
| v1.3 | 트레일링 스탑, NEUTRAL/REBOUND 장세, 바닥권 매수, 매수 모드 분리, 단일 전략 통합 |
| v1.4 | 1분봉 저장+리플레이 검증 인프라, 조건검색 3개 동시구독, 매도 점수제+구간형 트레일링, 재진입 제한, SKIP 사유 세분화 |
| v1.5 | 트레일링 구간 개편(손익비 개선), 진입 게이트 3종(추격매수/볼린저/상승여력), 재진입 1일1회→손실2회 게이트 전환, 제외종목 확대(인버스ETF), 리포트 정합성 버그 2건 수정, app.log 로테이션, 눌림목 하한 완화 |
| v1.6 | 포지션 상태머신(PSM) enforce 전환, Tracked Order Journal(재시작 시 미해결 주문 복원), 세션 지표 shadow, 진입 품질 shadow(VWAP 거리), 분봉 API 실측 진단, 거래비용 모델(Gross/Base/Stress) 단일화, FIFO 손익 계산 공통화, 리플레이 시간축 정합 |
| v1.7 | 외부 안전성 감사(F1~F10) 반영(보유종목 감시 누락·재시작 시 미해결주문 복원·malformed HTTP 응답 처리·잔고 페이지네이션 등), Candidate A 진입차단 게이트 A/B/C단계 거쳐 enforce 전환, 실행 기준선(run baseline) 기록 도입, 잔고 429 캐시 폴백 도입 후 안전 복구(스테일 잔고로 체결 오인 방지), entry_watch 관측(S02) 견고성 3라운드 보강(창 정의·기록 위치·손상값 방어) — **180초 감시 공백 해소는 진행 중**
