# legacy_20260721 기준선 fixture

**2026-07-24가 아니라 2026-07-21 데이터입니다.** 작업 환경(Claude
컨테이너)의 `data/minute_bars/`, `logs/signal_log.csv`,
`logs/trades.csv`를 확인한 결과 **7월 24일 데이터는 어디에도
존재하지 않았습니다**(가장 최근 날짜가 7월 21일). 지시받은 대로
데이터를 임의 생성하지 않고, 실제로 존재하는 가장 최근 거래일
데이터로 대체했습니다. 로컬에 7/24 데이터가 남아 있다면 알려주시면
다시 만들겠습니다.

## 이 fixture로 현재 할 수 있는 것 / 없는 것 (2026-07-27 갱신)

GPT 코드리뷰로 "의사결정 기준선 재현"과 "과거 자료 아카이브"가
서로 다른 완성도라는 지적을 받아 정확히 구분합니다.

| 단계 | 상태 |
|---|---|
| 1A-1 과거 로그·분봉 아카이브 | ✅ 완료 |
| 1A-2 fixture 구조·해시 무결성 검증 | ✅ 완료 (`manifest.sha256`, `test_legacy_fixture_structure.py`) |
| 1A-3 MinuteAnalyzer 재현 | ❌ 미완료 |
| 1A-4 StrategyRouter 재현 | ❌ 일봉 원본 부족으로 불가 |
| 1A-5 RiskManager 최종판단 재현 | ❌ 시점별 상태 부족으로 불가 (`risk_context_available: false`) |

**이 fixture는 아직 "의사결정을 재현해서 비교"하는 용도로 쓸 수
없습니다.** 지금 할 수 있는 건 (1) 과거 실제 판단 결과와 원본
분봉을 손상 없이 보존하는 것, (2) 그 보존 상태를 해시로 검증하는
것뿐입니다. 실제 재현 비교 테스트는 아래 "남은 제약"을 먼저 풀어야
가능합니다.

## 목적

"과거 로직이 옳다"는 걸 증명하려는 게 아니라, 1단계(세션/롤링
데이터 분리) 착수 전에 **현재 코드의 실제 동작을 정확히 동결**하기
위한 특성화 테스트(characterization test) 자료입니다.

## 구성

```
legacy_20260721/
├── README.md                          (이 파일)
├── manifest.sha256                    (재현 입력 파일들의 SHA-256 —
│                                        fixture가 조용히 변경되는
│                                        것을 감지하기 위함)
├── settings.yaml                      (config/settings.yaml 그대로,
│                                        민감정보는 ${ENV_VAR} 참조라
│                                        원본에도 실제 값 없었음)
├── minute_bars/
│   ├── 475150.csv   (320행, BUY/BLOCKED/눌림범위밖 케이스)
│   ├── 005930.csv   (319행, 점수부족/상승여력부족 케이스)
│   ├── 114800.csv   (269행, VWAP아래/지표없음 케이스)
│   ├── 252670.csv   (267행, 지표없음 케이스)
│   └── 012690.csv   (61행, 거래량없음 케이스)
├── end_of_capture_runtime_state.json  (data/state.json에서
│                                        last_order_id_by_symbol만
│                                        제거한 축소본 — 이름에
│                                        명시했듯 "판단 시점"이
│                                        아니라 "캡처 종료 시점"의
│                                        상태, 아래 경고 참고)
└── expected_decisions.json            (10개 판단 시점, cutoff
                                         메타데이터 포함)
```

## ⚠️ end_of_capture_runtime_state.json은 개별 판단 시점 재현에 쓸 수 없음

GPT 코드리뷰로 발견 — 이 파일은 `data/state.json`(작업 환경에
남아있던 최종 상태)을 그대로 가져온 것인데, 실제로 확인해보니
`475150`의 `last_sold_at`/`peak_price`/`symbol_stoploss_at`이 전부
**13:30:14**(오후) 시각을 담고 있습니다. 반면 fixture의 첫 BUY
판단은 **09:16:45**입니다 — 즉 이 파일은 "그날 하루가 끝난 뒤의
최종 상태"이지 "09:16 판단 시점의 상태"가 아닙니다.

이 파일을 그대로 써서 09:16 BUY를 재현하면, 실제로는 아직
일어나지 않았어야 할 그날 오후의 손절/매도 이력이 재진입 제한
판정에 잘못 반영됩니다. **개별 판단 시점의 RiskManager 재현
입력으로 사용하지 마세요** — 참고용 최종 상태로만 취급해야 합니다.

이런 이유로 `expected_decisions.json`의 각 케이스에는
`"risk_context_available": false`를 명시했습니다. 과거 각 시점별
정확한 `RuntimeState`(그 순간의 `bought_symbols_today`,
`symbol_entry_count_today` 등)를 복원할 자료가 없어서, 임의로
만들어 채우지 않고 "이 케이스는 RiskManager 최종판단까지는 재현할
수 없다"는 사실을 있는 그대로 표시했습니다.

## manifest.sha256 검증

`test_legacy_fixture_structure.py`가 이 manifest의 각 파일 해시를
실제로 다시 계산해서 일치하는지 검증합니다 — 파일이 조용히
수정되거나 손상되는 것을 잡기 위함입니다. manifest 자체는 검증
대상 목록에서 제외됩니다(자기 자신을 해시하지 않음).

## 실제로 확인된 사실 (1B 단계에서 참고할 것)

fixture를 만드는 과정에서 다음을 직접 확인했습니다 — 1B 단계
(raw_bars 진단)에서 검증할 항목들의 사전 단서입니다.

### 1. 저장된 분봉 CSV에 전일 봉이 섞여 있음

`475150.csv`(320행)를 날짜별로 세어보면:

```
20260720: 46건
20260721: 273건
```

`005930.csv`도 동일한 패턴(45건이 7/20). **저장 시점에 이미
전일 봉이 섞여 있었다**는 뜻입니다.

특히 `012690.csv`는 61건 중 겨우 2건만 7/21이고 나머지 58건이
7/20입니다. 이 종목의 실제 판단이 `SKIP_NO_VOLUME`이었는데,
어쩌면 "당일 데이터 자체가 거의 없어서" 나온 결과일 가능성이
있습니다(추측이며, 1B/1C 단계에서 실제로 검증 필요).

### 2. 저장된 CSV는 이미 시간순 정렬된 가공 데이터

`minute_bars/*.csv`는 브로커 원본 API 응답이 아니라, 이미
`KiwoomBroker.get_minute_bars()`가 `bars.reverse()`로 과거→최신
순 정렬을 마친 뒤 저장된 결과입니다. 1B 단계에서 확인해야 할
"API가 실제로 반환하는 원본 순서·개수"는 이 fixture로는 알 수
없고, 반드시 실제 API 호출 시점의 raw 응답을 별도로 로깅해야
합니다.

### 3. 일봉(daily bars) 캐시가 작업 환경에 없음

`data/`에 일봉 캐시 파일이 존재하지 않아, `RSI`/`MACD` 등 일봉
기반 지표의 원본 입력을 fixture에 포함하지 못했습니다. 대신
`signal_log.csv`에 이미 계산되어 기록된 지표값(`atr_14`,
`bb_percent_b`, `ma5_above_ma20` 등)을 `expected_decisions.json`에
그대로 보존했습니다 — 이 값들의 "재계산 검증"은 이 fixture로는
할 수 없고, "당시 기록된 값 자체의 동결"만 가능합니다.

### 4. 분봉 CSV에 판단 시점 이후(미래) 봉이 포함됨

GPT 코드리뷰로 발견하고 재현 확인 — 각 케이스의 판단 timestamp
기준으로 분봉을 세어보면, 예를 들어 `실제_BUY`(09:16:45)는 전체
319행 중 63행만 판단 시점 이전/동일이고 나머지 256행은 그 이후
(미래) 봉입니다. 향후 재현 테스트를 만들 때 CSV 전체를
`MinuteAnalyzer`에 그대로 넣으면 **미래 데이터 누출**이 발생합니다.

이 문제를 방지하기 위해 `expected_decisions.json`의 각 케이스에
다음 필드를 추가했습니다.

```json
{
  "decision_timestamp": "2026-07-21T09:16:45.503649",
  "bar_cutoff_timestamp": "20260721091645",
  "expected_bars_at_or_before_cutoff": 63
}
```

향후 재현 엔진은 반드시 `bar.cntr_tm <= bar_cutoff_timestamp`인
봉만 사용해야 합니다. 다만 실제 시스템이 판단 시점에 "진행 중인
분봉"까지 썼는지 "직전 완성봉까지만" 썼는지는 아직 확정하지
못했습니다 — 이건 1B 진단에서 raw 응답과 최신 봉 시각의 관계를
확인한 뒤 결정할 문제입니다.

### 5. legacy 실제 입력은 "cutoff 이전 전체"가 아니라 "cutoff 이전 중 최신 60개"

GPT 2차 코드리뷰로 발견 — 4번의 `bar_cutoff_timestamp`만으로는
아직 부족했습니다. 실거래 코드는 `config/settings.yaml`의
`minute_bar_count: 60` 설정으로 `raw_bars[:count]` 후
`reverse()`하므로, **실제 전략 입력은 "cutoff 이전 봉 중 최신
60개"**입니다. 장이 충분히 진행된 케이스(cutoff 이전 봉이 60개
이상)는 문제가 없지만, **장 초반 케이스는 cutoff 이전 전체가
60개 미만이라 전날 오후 봉까지 끌어와야 60개를 채웁니다.**

실제로 재현 확인한 결과, `실제_BUY`(09:16:45) 케이스의 legacy
60봉 입력창은:

```
개수: 60개
첫 timestamp: 20260720143900  (전날 7/20 14:39)
마지막 timestamp: 20260721091600  (당일 7/21 09:16)
전일 봉 포함: True
```

즉 **09:16의 VWAP/고가/저가 계산에는 실제로 전날 14시 39분 이후
데이터가 섞여 있었습니다.** 이건 세션 지표 오염 문제(1단계
착수 사유)를 fixture 데이터로 명확히 실증하는 증거입니다.

이 문제를 방지하기 위해 `expected_decisions.json`의 각 케이스에
다음 필드를 추가했습니다.

```json
{
  "legacy_requested_bar_count": 60,
  "expected_legacy_input_count": 60,
  "expected_legacy_first_timestamp": "20260720143900",
  "expected_legacy_last_timestamp": "20260721091600",
  "legacy_input_contains_prior_date": true
}
```

계산 규칙:

```python
eligible = [bar for bar in all_bars if bar.cntr_tm <= cutoff]
legacy_input = eligible[-legacy_requested_bar_count:]
```

10개 케이스 중 **6개**(개장 직후 케이스)가 실제로 전일 봉을
포함합니다: `실제_BUY`, `BLOCKED_재진입쿨다운`,
`SKIP_상승여력부족`, `SKIP_눌림범위밖`, `SKIP_거래량없음`.
나머지 4개(`BLOCKED_일일한도`, `SKIP_점수부족`, `SKIP_VWAP아래`,
`SKIP_지표없음_*`)는 시간이 충분히 지나 cutoff 이전 봉이 이미
60개를 넘어 전일 봉을 안 씁니다.

## expected_decisions.json의 10개 케이스

| case_label | timestamp | symbol | final_decision | 사유 | cutoff까지 봉 수 | legacy 60봉 전일포함 |
|---|---|---|---|---|---|---|
| 실제_BUY | 09:16:45 | 475150 | BUY | 정상 매수 신호 | 63 | True |
| BLOCKED_재진입쿨다운 | 09:28:42 | 475150 | BLOCKED | REENTRY_COOLDOWN | 75 | True |
| BLOCKED_일일한도 | 10:18:15 | 475150 | BLOCKED | MAX_ENTRIES_PER_DAY | 125 | False |
| SKIP_점수부족 | 10:00:23 | 005930 | HOLD | SKIP_SCORE_TOO_LOW | 106 | False |
| SKIP_VWAP아래 | 10:05:12 | 114800 | HOLD | NO_PAT_BELOW_VWAP | 61 | False |
| SKIP_상승여력부족 | 09:14:22 | 005930 | HOLD | SKIP_LOW_UPSIDE_REQUIRE5 | 60 | True |
| SKIP_눌림범위밖 | 09:13:35 | 475150 | HOLD | SKIP_PULLBACK_OUT_OF_RANGE | 60 | True |
| SKIP_거래량없음 | 09:01:49 | 012690 | HOLD | SKIP_NO_VOLUME | 60 | True |
| SKIP_지표없음_114800 | 10:05:00 | 114800 | HOLD | SKIP_NO_INDICATORS | 61 | False |
| SKIP_지표없음_252670 | 10:06:14 | 252670 | HOLD | SKIP_NO_INDICATORS | 61 | False |

각 행은 `logs/signal_log.csv`(7/21 실거래 로그)에서 그대로 가져온
원본 값입니다 — 필드명이 실제 의미와 다르더라도 가공 없이 원본을
보존했습니다.

## 남은 제약 (재현 테스트 착수 전 풀어야 할 것)

1. **일봉 지표 원본 부재**: RSI/MACD/MA5 재계산 불가, 기록값만 보존
2. **케이스별 RiskManager 상태 부재**: `risk_context_available: false`로
   명시된 케이스는 RiskManager 최종판단(재진입 제한 등)까지
   재현할 수 없음
3. **판단 시점의 "진행 중 봉 포함 여부" 미확정**: 1B 진단 이후 결정

## 참고: 원래 요청은 2026-07-24였음

작업 지시서에는 2026-07-24 데이터(실제 BUY 종목 006340/119850/
322000/475150)를 기준으로 하라고 되어 있었으나, 위에서 설명한
대로 작업 환경에 그 날짜의 원본 데이터가 없어 대체했습니다. 만약
민우님 로컬 환경에 2026-07-24의 `data/minute_bars/20260724/`나
`logs/signal_log.csv`(그날 데이터 포함)가 남아있다면, 그걸로 이
fixture를 다시 만드는 게 원래 지시에 더 정확히 부합합니다 —
그 경우 알려주시면 로컬 데이터를 업로드받아 다시 구성하겠습니다.
