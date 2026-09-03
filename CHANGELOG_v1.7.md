# 주식 자동매매 프로그램 ver1.7 패치노트

> 작성일: 2026-08-28
> 대상 기간: 2026-08-28(v1.6) ~ 진행중

---

## 작업 개요

v1.6(CHANGELOG_v1.6.md, 2026-07-24~2026-08-28)은 원래 계획했던
0~6단계 experimental flag 리팩터링에서 1P0.x 계열(체결 안전성)과
Profitability 계열(실거래 데이터 기반 손익 분석)로 초점이 이동하며
마무리됐습니다. 자세한 경위와 종료 시점 상태는 v1.6.md 맨 끝의
"요약(v1.6 종료)" 절을 참고하세요.

v1.7은 그 시점에서 이어집니다. 진입 시점의 **Candidate A Production
Pilot v1 A단계**(2026-08-28, `candidate_a_guard_mode="shadow"`
배포, `_try_buy()` enforce 게이트는 코드로 구현됐으나 아직 비활성)
가 v1.6 마지막에 이미 완료된 상태이고, v1.7의 첫 우선순위는 v1.6
말미의 "다음 버전 예정 방향" 표에 정리된 항목들입니다.

### v1.6에서 이어받는 미해결 항목

| 우선순위 | 항목 | 내용 |
|---|---|---|
| ✅ 완료 | Candidate A pilot B단계 | 2026-09-03 완료(APPROVE) — 001210/096770 미보유 자연실험 20건, 분봉 +5/+10분 100%·+20분 90% 확보. 아래 C단계 섹션 참고 |
| 🟡 중간 | MIN_PROFIT_5M 구조 분석 | 18건 중 15건 손실(-551,068원)의 진입시점 feature를 정상 청산 8건과 univariate 비교 — 아직 착수 전 |
| 🟢 낮음 | 원 6단계 계획 잔여분 재검토 | `decision_engine_mode`/`position_lifecycle_mode`/`reward_risk_guard_mode`/`candidate_ranking_mode`/`trailing_breakeven_mode` 5개 flag가 계속 off로 남아 있는 게 여전히 맞는 판단인지 정리 |

---

## 🔧 Candidate A Production Pilot v1 A단계 — reclosure (2026-08-28, 민우님 ZIP 직접 검토 지적 반영, production 로직 무변경)

### 배경

민우님이 A단계 diff를 직접 뜯어서 검토한 결과, 핵심 로직(gate 위치·
단일 evaluator·`"shadow"` 배포·BUY Signal 보존)은 명세 v1과 정확히
일치한다고 확인했습니다. 다만 C단계로 넘어가기 전에 정리하는 게
좋다며 작은 reclosure 4건을 지적했습니다 — 전부 A단계 shadow 배포
자체를 막는 P0는 아니지만, 이 프레임워크의 첫 enforce gate라서
지금 정리하는 게 맞다는 판단이었습니다.

### 변경 내용

1. **`SkipReason` 로컬 import 제거(근본 원인 수정)**: 이전 reclosure
   에서 Candidate A 분기에 로컬 `from infra.storage.skip_reason
   import SkipReason`을 추가해 `UnboundLocalError`를 막았는데,
   민우님 지적대로 이건 대증 처방이었습니다. 진짜 원인은
   `_try_buy()`의 MAX_POSITIONS 분기에 남아 있던 로컬 import가
   파이썬 스코프 규칙상 `SkipReason`을 함수 전체의 지역변수로
   만들어버린 것 — 이 원인 쪽 로컬 import를 제거했습니다(파일
   상단의 `from infra.storage.skip_reason import classify_skip_
   reason, SkipReason` 하나만 사용). Candidate A 분기에 추가했던
   방어용 로컬 import도 함께 제거.
2. **E2E 통합 테스트 추가(`test_candidate_a_guard.py` 8부, 15건
   신규)**: 기존 테스트는 `_try_buy()`를 단독 호출해 broker/PSM/
   journal에 side effect가 없는지만 확인했는데, 실제 운영 호출
   흐름(`_process_symbol()`)을 그대로 태워서 `low_upside_shadow.csv`
   한 행에 `would_skip_low_upside_no_spike=True` /
   `final_decision=BLOCKED` / `order_block_reason=
   SKIP_CANDIDATE_A_GUARD` / `order_attempted=False` /
   `order_accepted`·`order_id` 빈 값이 함께 기록되는지 신규
   확인했습니다. 전략의 `generate_signal()`만 BUY로 고정(breakout
   전략의 8개 진입조건을 다 맞출 필요 없이 관심사를 격리하기
   위함)하고, 장세 판단·분봉 분석도 시나리오에 맞게 고정했지만
   `_try_buy()`/`_write_signal_log()`/PSM/journal/broker는 전부
   실제 프로덕션 코드가 그대로 실행됩니다. off 모드 대조군(8-2)도
   추가해 같은 조건에서 관측은 계속되고 주문만 다르게 나가는지
   확인.
3. **차단 시 미소모 상태 4종 추가 검증**(`_pending_buy_side_effects`
   /`symbol_entry_count_today`/`bought_symbols_today`/`_last_buy_
   signal_at`) — 기존 3-3 테스트에 추가. 이 네 상태는 전부
   `_apply_first_fill_buy_side_effects()`(첫 실체결 확인 후)에서만
   채워지므로, Candidate A가 `broker.place_order()` 호출 전에
   반환하는 이상 소모될 수 없다는 걸 명시적으로 고정.
4. **`ExperimentalConfig.candidate_a_guard_mode` 주석 보강**:
   `candidate_a_guard_mode`는 `_try_buy()`의 주문 차단 gate
   승격 상태만 제어하고, `low_upside_shadow.csv` 관측은 이
   값이 `"off"`여도 `legacy_buy_candidate` 조건만 맞으면 항상
   그대로 기록된다는 예외를 명시(클래스 docstring의 일반 계약
   "off=새 로직 아예 미실행"과 다른 점).
5. **`candidate_a_guard.py` 날짜 오기 수정**: forward shadow 시작일
   설명을 `2026-08-27~`에서 실제 확정 경계인 `2026-08-26~`
   (`CANDIDATE_A_FORWARD_START_DATE`, tools/profitability_
   sprint.py)로 정정. 기능 영향 없음.

### 변경하지 않은 것

Predicate·gate 위치·`config/settings.yaml`의 `candidate_a_guard_
mode: "shadow"`·Candidate G/M1/MIN_PROFIT_5M 등 다른 매매 로직은
전혀 건드리지 않았습니다.

### 테스트 및 회귀 결과

`test_candidate_a_guard.py` 55/55 통과(기존 40건 + 신규 15건).
`python -m compileall` 클린. `run_regression_tests.py` 26/29
통과 — 실패 3건은 기존 환경 전용(무관, 신규 실패 0건).

---

## 🔬 Candidate A B단계 완료 + 2차 reclosure (2026-09-03, C단계 limited enforce pilot 승인 전 마지막 정리)

### B단계 — 미보유 종목 분봉 연속성 실측 (완료, 조건부 통과)

8/31 001210(11:20 청산 후 12:14/12:15/12:42에 Candidate A=True 재등장,
DAILY_ENTRY_LIMIT으로 차단)과 9/1 096770(10:35 청산 후 11:13부터
Candidate A=True가 17회 반복 관측, 전부 DAILY_ENTRY_LIMIT으로 차단) —
이 두 자연실험 종목의 실제 `data/minute_bars/*.csv`(민우님이 직접
첨부)를 대조해, 미보유/차단 상태에서도 분봉 수집이 계속됐는지 확인.

결과: 총 20건 이벤트 중 +5분/+10분은 20/20(100%), +20분은 18/20(90%)
확보. 결측 2건(096770 14:42/14:43)도 미보유 때문이 아니라 그 종목의
그날 분봉 수집 자체가 14:55에 끝난 것(`minute_bar_quality_20260901.txt`
공식 집계 356봉과 정확히 일치, 업로드본이 잘린 게 아님)이 원인 — B단계가
우려했던 "미보유 종목은 counterfactual 분석에 못 쓴다"는 문제는 이
표본에서는 확인되지 않음. 다만 종목 2개·20건뿐이라 일반화는 아직 이름
(전체 분석은 프로젝트 문서 `2026-09-03-candidate-a-stepB-analysis.md`
참고).

### 민우님 최종 판정 (2026-09-03)

- Candidate A B단계: **APPROVE — 완료**.
- Candidate A permanent adoption: 아직 판단 불가 (표본 부족, 별개 문제).
- Candidate A **limited C단계 enforce pilot: APPROVE** — 단, enforce
  배포 전 마지막 E2E/side-effect 통합 회귀(아래)를 먼저 통과시키는
  조건. `true-forward accepted n=4`(unique 4, cluster≈2)가 작다는
  이유만으로 pilot 자체를 계속 미루지는 않기로 함 — 이 표본 크기
  문제는 애초에 정했던 "졸업/재심사 기준"(actual blocks≥10, unique
  symbols≥5, independent clusters≥3, clean days≥3)이지 "enforce
  시작 전 채워야 할 문턱"이 아니었다는 게 근거.

### 2차 reclosure — enforce 최초 배포 전 마지막 E2E 통합 검증

1차 reclosure(위 A단계 섹션)의 8부 E2E(`_process_symbol()` 실제 호출
흐름)는 broker 포지션·trades.csv·low_upside_shadow.csv만 확인했고,
PSM/journal/`_pending_buy_side_effects`/entry_count/
`bought_symbols_today`/`_last_buy_signal_at` 6종은 3부(`_try_buy()`
단독 호출)에서만 검증하고 있었음 — 민우님 지적대로 "실제 BUY 후보 →
enforce 차단"이라는 한 흐름 안에서 전부 함께 고정되어 있지 않았음.
`test_candidate_a_guard.py` 8-1에 이 6종 검증을 통합 — 이제 8-1
하나가 다음을 전부 한 번의 `_process_symbol()` 호출로 확인함:

```
실제 BUY candidate + candidate_a_guard_mode=enforce + Candidate A=True
→ broker.place_order 미호출(포지션 없음)
→ PositionStateMachine = FLAT
→ tracked_order_journal 없음
→ _pending_buy_side_effects 없음
→ symbol_entry_count_today 증가 없음
→ bought_symbols_today 변화 없음
→ _last_buy_signal_at 변화 없음
→ low_upside_shadow.csv: would_skip_low_upside_no_spike=True /
  final_decision=BLOCKED / order_block_reason=SKIP_CANDIDATE_A_GUARD /
  order_attempted=False / order_accepted="" / order_id=""
```

`candidate_a_guard.py`의 forward 시작일 문서(`2026-08-26~`)는 1차
reclosure에서 이미 정정된 상태 그대로 — 이번엔 변경 없음.

### 테스트 및 회귀 결과

`test_candidate_a_guard.py` **61/61 통과**(기존 55건 + 신규 6건).
`python -m compileall` 클린. `run_regression_tests.py` 26/29 통과 —
실패 3건은 기존 환경 전용(`test_broker_order_status.py`,
`test_broker_read_only_wiring.py`, `test_replay_time_axis.py`, 전부
무관, 신규 실패 0건).

### 변경하지 않은 것

`config/settings.yaml`의 `candidate_a_guard_mode`는 이번에도
**여전히 `"shadow"`** — enforce 전환(C단계, 설정 한 줄)은 민우님이
이 reclosure 결과를 확인하신 뒤 별도로 명시 승인하면 진행합니다
(BUY/HOLD/SELL 로직 변경 전 확인 원칙). Predicate·gate 위치·Candidate
G/M1/MIN_PROFIT_5M/CRASH_CUT/score 등 다른 매매 로직은 전혀 건드리지
않았습니다.

---

## 🚀 Candidate A limited C단계 enforce pilot 시작 (2026-09-03, 민우님 명시 승인)

### 배경

2차 reclosure ZIP을 민우님이 직접 검토해 APPROVE(적용/커밋 가능) —
A/B단계는 더 막을 내용이 없다고 확인했습니다. 이어서 C단계 전환에
대해 다음을 명시 승인했습니다: "2차 reclosure 검토 완료,
APPROVE합니다. 이제 Candidate A limited C-stage enforce pilot 구현을
시작해주세요. 실제 전략 동작 변경은 config/settings.yaml의
candidate_a_guard_mode: "shadow" → "enforce" 한 항목으로만 제한합니다.
Candidate A predicate/gate 위치/threshold 및 다른 BUY/HOLD/SELL
로직은 절대 변경하지 마세요."

민우님이 추가로 짚은 점: `test_candidate_a_guard.py`의 배포 회귀
가드가 그대로 있으면 "shadow"를 기대하도록 고정돼 있어, 설정만
"enforce"로 바꾸면 그 즉시 61건 중 하나가 의도적으로 실패합니다.
따라서 실제 전략 동작 변경은 YAML 한 줄이 맞지만, diff 자체는
그 회귀가드 기대값과 관련 문구까지 함께 갱신해야 "문자 그대로 한
줄짜리"가 되지 않고 정상적으로 닫힙니다.

### 변경 내용

1. **`config/settings.yaml`**: `candidate_a_guard_mode: "shadow"` →
   `"enforce"`. 바로 위 주석을 B단계 완료·C단계 승인 경위, freeze
   목록, 새 평가 단위(candidate_a_guard actual blocks)와 졸업/재심사
   기준(actual blocks≥10, unique symbols≥5, independent clusters≥3,
   clean days≥3)으로 갱신.
2. **`test_candidate_a_guard.py`**:
   - 모듈 docstring 상단을 C단계 승인 경위로 갱신(과거 "A단계까지만
     승인, enforce는 코드 검증만" 문구를 "C단계 limited enforce pilot
     시작, 실제 변경 범위는 설정 한 줄" 문구로 정정).
   - 4부 설명 및 4-1 테스트를 **"shadow"가 배포됐는지 확인**하던
     것에서 **"enforce"가 배포됐는지 확인**하는 것으로 반전 — 이제
     "shadow"로 되돌아가 있으면 승인된 pilot이 실수로 꺼진 것으로
     판정합니다.
3. **`CHANGELOG_v1.7.md`**(이 문서): B단계 완료 항목을 ✅로 갱신,
   이 섹션 추가.

### 변경하지 않은 것

Candidate A predicate(`upside<0.50 AND rebound_volume_spike is
False`)·`_try_buy()` gate 삽입 위치(RiskManager → Candidate A → PSM →
broker.place_order)·threshold 상수·다른 모든 매매 로직(Candidate
G/M1/MIN_PROFIT_5M/CRASH_CUT/entry score/BULLISH 판정/동시진입
제한)·Broker/lifecycle은 전혀 건드리지 않았습니다. **최소 3~5 clean
거래일 동안 이 동결을 유지합니다** — Candidate A 하나만 실제 개입시켜야
성과 귀속이 가능하다는 민우님 지시에 따른 것입니다.

### 테스트 및 회귀 결과

`test_candidate_a_guard.py` **61/61 통과**(4-1이 이제 "enforce" 배포를
확인하는 것으로 반전됐고, 다른 60건은 이전과 동일). `python -m
compileall` 클린. `run_regression_tests.py` 26/29 통과 — 실패 3건은
기존 환경 전용(`test_broker_order_status.py`,
`test_broker_read_only_wiring.py`, `test_replay_time_axis.py`, 전부
무관, 신규 실패 0건).

### 다음 단계

enforce 전환 이후부터는 accepted n을 더 기다리지 않고, 평가 단위를
`candidate_a_guard` 실제 차단(actual blocks) 건수로 전환합니다.
차단마다 기존 minute_bars로 +5/+10/+20분 forward proxy를 계산하고,
동일 종목·반복 폴링에서 나온 이벤트는 cluster/dedup 기준으로 묶어
독립 표본으로 과대계상하지 않습니다. 졸업/재심사 기준(actual
blocks≥10, unique symbols≥5, independent clusters≥3, clean days≥3)을
채우면 permanent adoption 여부를 별도로 재심사합니다.

---

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->
