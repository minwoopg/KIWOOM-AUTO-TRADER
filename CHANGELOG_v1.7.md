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

## 🛡️ 외부 안전성 감사(2026-09-07) 검토 후 채택 — P0/P1 인프라 버그 수정 (전략 로직 무변경)

### 배경

민우님이 별도 경로로 받은 외부 안전성 감사(`KIWOOM_AUDIT_20260907.md` +
`KIWOOM_safety_review_20260907.patch` + `KIWOOM_audit_evidence_20260907.zip`,
기준 `main@a5106df`)를 검토·적용해달라고 요청. 감사는 8/26~9/4 8일·26회
왕복거래의 주문가 추정 기대값이 비용 전부터 음수(-12,677원/거래, Base
비용 후 -33,334원/거래)이며, 실제 체결가·비용 원장이 없어 실현 손익은
계산 불가하다는 별도 수익성 결론과, 재시작·잔고캐시·HTTP 응답 파싱 등
운영 안전성 P0/P1 버그 10건(F1~F10)을 담고 있었음. 이번 항목은 **코드
수정 부분만** 다룸(수익성 분석은 프로젝트 문서
`kiwoom-auto-trader/2026-09-07-external-safety-audit-review.md` 참고).

### 독립 검증 절차 (Claude, 적용 전)

1. 실제 GitHub `main`을 별도로 clone해 감사가 명시한 기준 커밋
   `a5106dfba031ab1440d0ba9c4f0238fffd1bdfa7`와 일치함을 직접 확인.
2. 첨부 patch를 그 clone에 `git am`으로 적용 — 충돌 없이 5개 커밋 전부
   적용됨.
3. 수정 전/후 `run_regression_tests.py` 직접 실행:
   - 수정 전: 29개 파일 중 26 PASS·3 FAIL
     (`test_broker_order_status.py`, `test_broker_read_only_wiring.py`,
     `test_replay_time_axis.py`)
   - 수정 후: 30개 파일 중 28 PASS·2 FAIL
     (`test_broker_read_only_wiring.py`만 해결, 신규 실패 0건) —
     감사 보고서의 주장과 정확히 일치.
4. 신규 `test_review_safety_regressions.py` 25/25 PASS 직접 실행 확인.
5. 일부 재현 로그(`reproductions_before.txt`)를 수정 전 코드에 대해
   재실행해 F1(보유 종목 감시 누락)·F3(malformed HTTP 응답)·F6(재시작
   시 당일 리셋) 실패가 실제로 재현됨을 확인.
6. `git diff --stat`으로 14개 변경 파일 목록을 확인 — `domain/strategy/`,
   `config/settings.yaml`, `config/settings.py`, `domain/market_regime/`
   등 전략 predicate·threshold·설정 파일은 **단 한 줄도 포함되지
   않음**을 직접 확인.

### 채택한 수정 (F1~F5, F7~F10 + F6의 재시작 버그 부분)

- 보유 종목이 조건검색 targets/excluded_symbols/UNKNOWN 3연속 제외에
  걸려도 손절 감시에서 빠지지 않도록 수정(F1).
- 재시작 시 미확인 주문 상태를 journal에서 복원해 중복 주문을 막고,
  journal을 읽을 수 없으면 자동 주문을 전부 차단(fail-close)(F2).
- 브로커 HTTP 200 응답의 `return_code`가 정상 int가 아니면 확정
  거절이 아닌 ambiguous로 처리(F3).
- 계좌에 미확정 주문이 남아있는 동안 신규 진입을 직렬화, 실시간 잔고
  조회 실패 시 신규매수 차단(F4).
- 미확정 주문 동안 180초 잔고 캐시를 우회(F5).
- **재시작이 당일 거래일을 리셋하지 않도록 `RuntimeState.roll_
  trading_day()` 신설**(F6의 절반만 — 아래 "채택하지 않은 부분" 참고).
- 잔고 페이지네이션 누락·음수 현금 처리 보정(F7).
- 시장 개장/마감 판정을 호스트 타임존이 아닌 KST 기준으로 통일,
  주말 제외, 장 마감 후에도 미확정 주문 대조는 계속(F9).
- 현재가 캐시가 오래됐거나 미래 시각이면 신규 매수 차단(F10).
- `MockBroker`가 부분매도·재조회를 정확히 흉내내도록 보강, 프로세스
  중복 실행 방지용 OS 파일 잠금 신설.
- F8(손익 한도가 체결가 아닌 주문가 기준)은 감사도 명시적으로
  **미해결**로 분류 — 이번에도 손대지 않음.

### 채택하지 않은 부분 — 민우님 명시 결정

감사 원안은 F6에 "같은 종목 반복손실도 전역 `consecutive_losses`에
반영"하는 수정을 포함했음. 이는 2026-06-15에 민우님이 이미 의도적으로
내린 결정(한 종목의 불운이 계좌 전체 매수를 막지 않도록, 전역
카운터는 종목별 **첫** 손실만 반영하고 반복 손실은 `symbol_loss_
count_today`로 그 종목만 별도 차단)을 뒤집는 것. `max_consecutive_
losses=3`이므로 원안대로면 한 종목이 하루 3번만 손절해도 계좌 전체
신규매수가 중단됨.

민우님 검토 결과: **2026-06-15 정책을 그대로 유지**하기로 결정 —
`if cnt == 1: self.state.consecutive_losses += 1`를 그대로 보존.
재시작 시 당일 상태가 리셋되는 버그 수정(F6의 나머지 절반)만 채택.
실측 데이터 확인: 감사가 분석한 8일·26건 중 같은 날 같은 종목이
2번 이상 거래된 사례는 0건이므로, 이번 결정은 과거 어떤 날의 결과도
바꾸지 않음 — 앞으로의 방향성 선택.

이 결정에 맞춰 `test_review_safety_regressions.py`의 관련 테스트를
`test_symbol_loss_count_ignores_duplicate_observation_global_counter_
kept_2026_06_15_policy`로 이름 변경하고 기대값을
`consecutive_losses == 2` → `== 1`로 수정, `docs/SAFETY_REVIEW_
20260907.md`도 이 결정을 반영해 갱신.

### 최종 검증 (F6 수정 후 재실행)

`test_review_safety_regressions.py` **25/25 PASS**(변경 후에도 유지),
`run_regression_tests.py` **30개 파일 중 28 PASS·2 FAIL**(변경 전과
동일, 신규 실패 0건), `python -m compileall` 클린.

### 변경하지 않은 것

Candidate A predicate·gate·threshold, Candidate G/M1/MIN_PROFIT_5M/
CRASH_CUT/entry score/BULLISH 판정, `config/settings.yaml`/`config/
settings.py`의 모든 전략 파라미터, `domain/market_regime/` 분류기 —
전부 무변경. F8(손익 한도 회계)도 미해결로 남김.

### 전달 파일

- `KIWOOM_safety_review_20260907_reviewed.patch` — 감사 원본 5개
  커밋 + F6 조정 1개 커밋(총 6개), 기준 커밋(`a5106df`)에 `git am`
  적용 검증 완료.
- 14개 파일 전체 덮어쓰기(diff zip) — `app/main.py`, `domain/models.py`,
  `domain/service/trading_service.py`, `infra/broker/{base,kiwoom_
  broker,mock_broker}.py`, `infra/storage/{process_lock(신규),
  state_reconciler,state_store}.py`, `utils/time_utils.py`,
  `test_review_safety_regressions.py`(신규),
  `test_tracked_order_journal.py`, `tools/audit_trade_bundles.py`
  (신규, 감사용 분석기), `docs/SAFETY_REVIEW_20260907.md`(신규).

---

## 🛟 GPT 재검토 3라운드 반영 — 잔고 429 폴백 안전 복구 + S02 관측 견고성 보강 (2026-09-14)

### 배경

민우님이 커밋 `68aef3a`(잔고 429 폴백(P0-1) + 잔고 신선도·지연평가
후보 관측 로그)를 GPT에 재검토 요청. 3라운드에 걸쳐 총 7건의 결함이
확인·수정됐습니다. 전략 로직(RSI·MACD·진입점수·predicate)은 이번
라운드에서 전혀 건드리지 않았습니다 — 전부 잔고 조회/관측 인프라
범위입니다.

### 1라운드 — 429 폴백이 스테일 잔고를 체결 확인으로 오인시키는 위험

`_get_balance_with_cache()`의 P0-1 폴백(429 시 직전 캐시 잔고 반환)이,
미해결 주문(BUY_PENDING 등)이 있어 반드시 최신 잔고가 필요한 바로 그
분기에서 429가 나면 그 캐시값이 `_sync_position_state_machine_shadow()`
→ `confirm_buy_from_broker()`로 그대로 흘러가 **오래된 수량을 "이번
체결 확인 결과"로 오인**시키는 경로가 재현됨.

**수정**: 폴백 자체를 되돌림 — 429든 다른 예외든 캐시 존재 여부와
무관하게 예외를 그대로 전파(2026-08-10 이전과 동일 동작). 관측 로그
(`balance_freshness.csv`)는 유지하되 새 outcome 값
`fetch_failed_429_fallback_disabled` 추가. **이로 인해 180초 감시
공백 문제가 다시 열린 채로 남으며, 이는 별도 과제로 처리합니다**
(아래 "다음 작업" 참고).

### 1라운드(계속) — S02 지연평가 후보 관측 결함 2건

- entry_watch의 "평가 이력" 기록이 `elapsed_min ≤ watch_minutes+1`인
  아무 시점에서나 발생해, 매수 1~2분 뒤에도 "5분 시점 최소수익 평가
  완료"로 잘못 기록되던 문제 → `elapsed_min >= watch_minutes`로만
  기록하도록 좁힘.
- 이전 진입의 기록이 청산·정리 안 된 채 남아 있으면 재진입 시 새
  진입의 관측을 가려버리는 문제 → 저장값을 `entry_time|기록시각`
  형식으로 바꿔 현재 진입 건과 일치할 때만 "이미 평가함"으로 인정,
  공식 청산 처리(`_apply_deferred_sell_side_effects()`)에서 정리
  로직 추가.

### 2라운드 — 평가 이력 기록 위치가 급락·VWAP 조기반환보다 앞에 있음

급락(fail_cut)이나 VWAP 이탈로 먼저 SELL을 반환하는 경우에도 평가
이력 기록 블록이 그보다 먼저 실행돼, 실제로는 최소수익 비교 자체에
도달하지 않았는데 "평가 완료"로 기록되던 문제.

**수정**: 기록 블록을 두 분기 뒤, 실제 최소수익 조건 비교 직전으로
이동. 급락·VWAP 조기반환 시 평가 이력을 남기지 않고, 실제 비교에
도달한 경우(수익 충족/미달 무관)에만 기록하도록 테스트로 고정. 기존
SELL 판단 순서·반환값은 변경 없음.

### 3라운드 — S02 관측 필드 자체의 견고성 결함 3건

1. **(최우선) 손상된 관측값이 SELL 판단 자체를 막을 수 있는 결함** —
   저장값 파싱(`.partition("|")`)에 타입 검증·예외 처리가 없어, 상태
   파일에 `null`/숫자 등이 들어오면 `_check_entry_watch()`가
   `AttributeError`로 죽어 SELL 판정 자체에 도달하지 못하는 경로가
   `JsonStateStore.load()`부터 실제로 재현됨. → 어떤 입력에도 예외를
   던지지 않는 `_parse_normal_eval_seen()` 헬퍼로 통일, 기록 블록
   try/except 격리, `JsonStateStore.load()`에서도 문자열이 아닌 값은
   걸러내는 이중 방어 추가.
2. **무효 가격(0 등)이 dedup 슬롯을 소모하는 결함** — 지연 후보 로거가
   `current_price=0` 같은 무효 가격도 `(symbol, entry_time)`당 1회뿐인
   dedup 슬롯으로 기록해, 이후 도착하는 유효 가격 관측이 조용히
   버려짐. → `price_valid` 판정을 추가해 무효 가격은 dedup 없는
   `append()`로, 유효 가격만 `append_if_new()`로 분리. `price_
   observed_at`/`price_age_seconds` 필드도 추가.
3. **구형식(entry_time 미결합) 값이 "진짜 누락"과 섞이는 문제** —
   업그레이드 이전 값은 안전하게 "아직 못 봄"으로 처리되지만, 배포
   경계에서 지연 후보 통계에 "진짜 누락"과 섞여 부풀려질 수 있음. →
   `prior_seen_format`(`none`/`current`/`legacy`/`invalid`) 필드로
   구분해 기록.

### 테스트 및 검증

`test_delayed_eval_candidate_observation.py` 36개(신규 3그룹 포함),
`test_balance_429_fallback.py`/`test_balance_freshness_observation.py`
갱신. 매 라운드 GitHub main 새 클론에 순서대로 `git am` 적용해 독립
검증 — 최종 4-패치 체인 적용 후 `run_regression_tests.py` **37개 중
36 PASS**(무관한 기존 실패 1건 동일), `legacy_tests/test_entry_watch.py`
**11/11 PASS**, `git status --short` 클린.

### 변경하지 않은 것

RSI·MACD·진입점수·predicate·Candidate A/G/M1/MIN_PROFIT_5M/CRASH_CUT
등 모든 매매 판단 로직은 전혀 건드리지 않았습니다. 이번 라운드는
잔고 조회 실패 처리와 순수 관측 필드의 방어 로직에 한정됩니다.

### 다음 작업

**180초 감시 공백 해소**가 다음 최우선 과제로 남아 있습니다. 429
폴백을 되돌리면서 다시 열린 문제로, (1) 잔고 조회 재시도 대기를 전체
감시 루프에서 분리, (2) 과거 잔고로 체결·주문 종결을 확정하지 않기,
(3) 시세 감시 지속과 실제 청산 가능 여부를 구분해 검증 — 이 세 목표로
설계를 진행 중이며, 매도 주문 제출 경로에 직접 영향을 주는 변경이라
구현 전 별도 확인을 거칩니다.

### 전달 파일

`0001-fix-risk-429-fallback-revert.patch` ~
`0004-fix-obs-S02-defensive-validation.patch` (4개, 순서대로 `git am`),
각 라운드별 diff zip. 상세 배경은 프로젝트 문서 `2026-09-14-gpt-review-
fixes.md`/`-v2.md`/`-v3.md` 참고.

---

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->

## 🔧 180초 감시 공백 대응 1단계 — 손절·트레일링 계산 추출 (2026-09-14, GPT 재검토 2회 반영, 매매 판단 로직 무변경)

### 배경

앞서 예고된 "180초 감시 공백 해소" 작업의 설계 문서(v1/v2)와 구현
지시서를 GPT 재검토와 함께 확정하는 과정에서, 잔고 장애 중 관측
전용 경로가 "고정 손절"과 "트레일링"만 평가하려면 `Strategy.
generate_signal()` 전체를 재사용할 수 없다는 지적을 받았습니다 —
그 함수는 VWAP 이탈·추세 꺾임·안전망 익절까지 함께 계산해서
반환하므로, 반환된 SELL을 그대로 기록하면 정한 범위를 넘어섭니다.
`reason` 문자열에 "손절"/"트레일링"이 들어있는지 검사해 걸러내는
방식도 문구 변경에 취약해 채택하지 않았습니다. 실제 관측 경로를
구현하기 전에 이 선행 리팩터를 먼저 해결했습니다.

### 변경 내용

1. `domain/strategy/exit_calc.py` 신설 — `calc_stop_loss()`/
   `calc_trailing_stop()` 순수 함수. 판정에 필요한 수치(트리거 여부,
   손절가/트레일링 스탑가, 적용된 트레일링 폭 등)만 반환하고
   `reason` 문자열·수익률(`current_pnl_pct`)은 계산하지 않습니다 —
   문구는 전략마다 달라 각 전략 파일이 계속 담당하고, 수익률은
   reason 조립에만 쓰이는 값이라 필요한 전략이 직접 계산합니다
   (아래 1차 검토 지적 1번 참고).
2. `BreakoutStrategy`/`BottomStrategy`/`NeutralStrategy`/
   `HoldStrategy` 4개 파일이 기존에 각자 갖고 있던 손절·트레일링
   계산을 이 공용 함수 호출로 교체. 임계값·반올림·평가 순서·reason
   문자열은 전부 기존과 동일 — 전략별 트레일링 방식 차이(Breakout
   4단계 구간/Neutral 4단계지만 다른 경계값/Bottom 단일 폭/Hold는
   트레일링 없음)도 그대로 유지했습니다.

### 1차 검토(GPT) 지적 반영 — 완료 처리 전 수정 2건

1. **HoldStrategy에 새로 생긴 ZeroDivisionError**: 초기 버전의
   `calc_stop_loss()`가 손절 판정에 쓰지도 않는 수익률
   (`current_pnl_pct`)까지 계산했는데, 원본 `HoldStrategy`는 이
   값을 아예 계산하지 않았습니다(익절/손절 가격만 비교). 평균단가
   0인 입력에서 원본은 크래시 없이 SELL(익절)을 반환했지만, 리팩터
   버전은 이 나눗셈 때문에 `ZeroDivisionError`로 죽는 회귀가
   생겼습니다 — `calc_stop_loss()`에서 수익률 계산을 제거하고 필요한
   3개 전략(Breakout/Bottom/Neutral)이 원본처럼 직접 계산하도록
   되돌려 수정했습니다. 이 3개 전략은 원본도 손절 검사 전에 수익률을
   무조건 계산했으므로 평균단가 0 크래시가 그대로 남아있는데, 이는
   새 회귀가 아니라 리팩터 이전부터 있던 동작이라 이번 범위에서
   손대지 않았습니다(무효 평균단가 처리 정책은 별도 항목).
2. **신규 테스트가 전체 회귀에서 실행되지 않던 문제**:
   `run_regression_tests.py`는 pytest가 아니라 각 `test_*.py`를
   `subprocess`로 그대로 실행하는 방식인데, 처음 작성한 pytest 스타일
   클래스(직접 실행 진입점 없음)는 아무 것도 실행하지 않고 조용히
   종료 코드 0으로 끝나 "PASS"로 오인될 수 있었습니다. 프로젝트 관례
   (`unittest.TestCase` + `if __name__ == "__main__": unittest.main()`)에
   맞춰 다시 작성 — 이제 직접 실행 시 "Ran N tests"가 출력되고,
   assertion을 인위로 실패시키면 `run_regression_tests.py`도 해당
   파일을 FAIL로 표시함을 확인했습니다. 손절·트레일링 참조 함수의
   HOLD 경로(트레일링 추적 중/보유 유지) 일부가 비교 대상에서 빠져
   있던 것도 채웠고, 평균단가를 2가지 값(정수 배 아닌 값 포함)으로
   확장했습니다.

### 테스트 및 검증

`test_exit_calc_equivalence.py`(10건) — 4개 전략 모두 손절가·
트레일링 시작가·구간 경계(수익률 0.5/1.0/1.2/2.0/3.0/3.5/5.0%) 근방을
촘촘히 스캔하며, 리팩터 이전 공식을 exit_calc.py와 무관하게 손으로
다시 옮겨 적은 참조 구현과 SELL/HOLD 타입·reason 문자열이 정확히
일치하는지 검증(순환 검증 아님, HOLD 경로 포함). 트레일링 스탑가
바로 아래/동일/바로 위 경계, HoldStrategy 평균단가 0 회귀 고정
테스트 포함. `python test_exit_calc_equivalence.py` 직접 실행으로
"Ran 10 tests / OK" 출력 확인.

`run_regression_tests.py` 38개 중 37 PASS(`test_broker_order_status.py`
fixture 누락은 변경 전 main에서도 동일하게 실패 — `git stash`로
확인, 무관). `legacy_tests/test_entry_watch.py` 11/11 PASS.

### 변경하지 않은 것

RSI·MACD·진입점수·predicate 등 매수 판단 로직과, 각 전략의 손절·
트레일링 임계값·구간 경계·평가 순서는 전혀 바꾸지 않았습니다 —
순수하게 같은 계산을 공용 함수로 옮긴 것입니다. 잔고 장애 중 관측
경로 자체(재시도 스케줄러, 조회/확정 분리, 주문 증거 저장, 최고가
후보 병합 등)는 이번 커밋에 포함하지 않았습니다.

### 별개 발견 — 정정: 운영 크래시 아님 (이번 작업 범위 밖, 손대지 않음)

`BottomStrategy.generate_signal()`이 `domain/models.py`의
`MarketPrice`에 더 이상 존재하지 않는 필드(`indicator_rsi_signal_
cross`/`indicator_volume_exhaustion`/`indicator_volume_buying`)를
position 분기보다 앞에서 무조건 참조합니다 — **직접 호출하면**
`AttributeError`가 재현됩니다. git blame으로 확인: 2026-05-21 커밋
`8093acd`("NEUTRAL 장세 추가")에서 관련 없는 `models.py` 정리 작업
중 실수로 삭제된 것으로 보입니다.

**정정(1차 검토에서 지적)**: 처음에는 "REBOUND 장세가 분류되면
운영 중 즉시 크래시한다"고 적었는데, 이는 부정확했습니다.
`StrategyRouter.select()`를 직접 확인한 결과 REBOUND는 이미
`BottomStrategy`가 아니라 `NeutralStrategy`로 우회되어 있습니다
(`# BottomStrategy는 MarketPrice 필드 미완성 — 임시로 NeutralStrategy
사용`). 즉 현재 운영 경로에서는 이 크래시가 발생하지 않고, **비활성
상태인 BottomStrategy를 직접 호출할 때만** 재현되는 결함입니다.
이번 작업 범위 밖이라 손대지 않았고(누락 필드에 임의 기본값을
채우거나 라우터를 바꾸지 않음), 테스트에서는 `object.__setattr__`로
임시 우회만 했습니다. `BottomStrategy`를 실제 활성화하려면 지표
생성 경로까지 함께 검토가 필요합니다 — 다음 관측 경로 구현에서도
REBOUND를 이름만 보고 BottomStrategy 설정에 연결하지 않아야 합니다.

### 다음 작업

이 추출이 승인되면 구현 지시서 §2.5(장애 중 청산 후보 계산)를 이
`calc_stop_loss()`/`calc_trailing_stop()`을 직접 호출하는 관측 전용
평가 함수로 구현합니다. 이때 계산 함수뿐 아니라 전략별 트레일링
시작 배율·구간표(tiers)도 `StrategyRouter`가 실제로 선택하는 전략을
따라 한 곳에서 공유해야 합니다 — 관측 코드에 구간표를 별도로
복사하면 나중에 정상 전략과 관측 결과가 어긋날 수 있습니다.
재시도 스케줄러(단조 시계 기반)·주문 증거의 주문번호 연결·최고가
후보의 진입 건 식별 병합 비교는 계속 별도의 작은 커밋으로 나눠
진행합니다.

### 전달 파일

`0001-refactor-strategy-exit_calc.py.patch`,
`0002-docs-CHANGELOG-v1.7.patch`, 관련 파일 전체가 담긴 diff zip.
상세 배경은 프로젝트 문서 `2026-09-14-180s-gap-design-doc.md`/
`-v2.md`, `2026-09-14-180s-gap-implementation-directive.md`,
`2026-09-14-180s-gap-step1-exit-calc-extraction.md` 참고.

---

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->

## 🔧 180초 감시 공백 대응 2단계 — 전략별 트레일링 설정 공유 + 청산 후보 계산 (2026-09-14, 매매 판단 로직 무변경)

### 배경

1단계(손절·트레일링 계산 추출)는 개발 검증 완료로 승인됐습니다.
승인과 함께 받은 다음 단계 지시는 두 가지였습니다: (1) 전략별
트레일링 시작 배율·구간표(tiers)를 잔고 장애 관측 코드에 다시
복사하지 말고 계산 함수와 함께 한 곳에서 공유할 것, (2) 실제
`StrategyRouter`가 선택하는 전략을 기준으로 청산 후보를 계산할 것
(REBOUND를 이름만 보고 BottomStrategy로 임의 연결하지 않을 것).
이번 커밋은 이 두 가지만 구현합니다 — 잔고 장애 중 실제 주문 제출과
최고가 후보의 운영 상태 병합은 이전과 동일하게 비활성 상태입니다.

### 변경 내용

`domain/strategy/base.py`의 `Strategy` 추상 클래스에
`trailing_params() -> TrailingParams | None` 메서드를 추가했습니다
(기본값 `None` = 트레일링 없음). `domain/strategy/exit_calc.py`에
`TrailingParams`(시작 배율 + 구간표)와 `ExitCandidate`(kind +
StopLossResult + TrailingResult|None) 데이터클래스, 그리고
`evaluate_exit_candidate(strategy, average_price, current_price,
highest_price, stop_loss_pct)` 함수를 추가했습니다 — 손절을 먼저
평가하고(모든 전략의 원본 순서와 동일), 트리거되지 않으면
`strategy.trailing_params()`로 그 전략이 실제 쓰는 값을 꺼내
`calc_trailing_stop()`에 그대로 넘깁니다. `strategy.generate_signal()`
은 호출하지 않습니다 — VWAP 이탈·추세 꺾임·안전망 익절까지 함께
평가되면 "손절·트레일링만 본다"는 범위를 넘어서기 때문입니다.

`BreakoutStrategy`/`NeutralStrategy`는 트레일링 시작 배율·구간표를
클래스 상수(`_TRAILING_START_MULTIPLIER`/`_TRAILING_TIERS`)로 한
곳에만 두고, `trailing_params()`와 `generate_signal()`의
`calc_trailing_stop()` 호출부가 둘 다 이 상수를 읽도록 했습니다.
`BottomStrategy`는 트레일링 폭이 `config.trailing_stop_pct`에 달려
있어 고정 상수로 둘 수 없으므로, `trailing_params()`가 매 호출마다
`self.config`에서 값을 읽어 `TrailingParams`를 만듭니다(시작 배율
`1.03`만 상수). `HoldStrategy`는 원래 트레일링이 없으므로
`trailing_params()`를 명시적으로 오버라이드해 `None`을 반환합니다
(기본값과 동작은 같지만, 파일만 보고도 "트레일링 없음"을 알 수
있도록 명시).

### 테스트 및 검증

`test_exit_calc_equivalence.py`에 9건을 추가했습니다(총 19건).
`TestTrailingParamsSingleSource`(4건): `BreakoutStrategy`/
`NeutralStrategy`의 `trailing_params()`가 기존 하드코딩 값과 정확히
일치하는지, `BottomStrategy`는 서로 다른 `config.trailing_stop_pct`로
만든 두 인스턴스가 서로 다른 tiers를 반환하는지(고정 상수가 아님을
확인), `HoldStrategy`는 `None`을 반환하는지 확인. `TestEvaluate
ExitCandidate`(5건): `StrategyRouter.select(REBOUND)`가 반환하는
객체가 실제로 `NeutralStrategy`인지 확인하고 그 파라미터(1.005배)로
계산되는지, 손절이 트레일링보다 우선하는지, 트레일링 후보가
정상적으로 나오는지, 아무 조건도 없으면 `None`인지, `HoldStrategy`가
선택된 장세(SIDEWAYS/BEARISH/UNKNOWN)에서는 `highest_price`를
극단적으로 줘도 `TRAILING` 후보가 절대 나오지 않는지(STOP_LOSS 또는
None만 가능) 확인.

기존 10건(1단계 동등성 테스트)은 이번 리팩터(트레일링 호출부를
클래스 상수/메서드 참조로 변경) 이후에도 전부 그대로 통과함을
확인했습니다 — `generate_signal()`의 실제 출력이 바뀌지 않았다는
뜻입니다. `python test_exit_calc_equivalence.py -v` 직접 실행으로
"Ran 19 tests / OK" 확인. 인위로 `NeutralStrategy`의 시작 배율
상수를 틀린 값으로 바꿔 2건(동등성 1건 + 신규 trailing_params 1건)이
FAIL로 표시되고 종료 코드 1이 나오는 것, `run_regression_tests.py`도
`test_exit_calc_equivalence.py`를 FAIL로 표시하는 것을 확인한 뒤
원복했습니다. `run_regression_tests.py` 38개 중 37 PASS(1단계와
동일한 `test_broker_order_status.py` fixture 누락, 무관).
`legacy_tests/test_entry_watch.py` 11/11 PASS.

### 변경하지 않은 것

각 전략의 손절·트레일링 임계값·구간 경계·시작 배율·평가 순서는
전혀 바꾸지 않았습니다 — 기존에 파일마다 흩어져 있던 리터럴을
클래스 상수/메서드 하나로 모았을 뿐입니다. 매수 판단 로직(RSI·
MACD·진입점수 등)도 손대지 않았습니다. `BottomStrategy`의
`MarketPrice` 필드 누락 문제(1단계에서 발견·정정)는 이번에도
그대로 두었습니다 — REBOUND는 여전히 `StrategyRouter`가
`NeutralStrategy`로 우회합니다. **실제 주문 제출과 최고가 후보의
운영 상태(트레일링 기준선) 병합은 이번에도 활성화하지 않았습니다**
— `evaluate_exit_candidate()`는 순수 계산 결과만 반환하고, 어디에도
연결되어 있지 않습니다. 재시도 스케줄러(단조 시계 기반)·주문 증거의
주문번호 연결도 별도 작업으로 남아 있습니다.

### 다음 작업

이번 계산 함수를 실제 잔고 장애 관측 루프에 연결해 로그·기록만
남기는 단계로 진행합니다(주문 제출 없음). 이후 구현 지시서 §2의
나머지 항목(재시도 스케줄러 독립 백오프, 주문 상태 조회/확정
분리, 최고가 후보의 진입 건 식별 병합 — 이건 여전히 "관측 전용"이며
운영 트레일링 기준선에 병합하는 것은 별도 정책 승인 대상)을 계속
작은 커밋으로 나눠 진행합니다. 이번 작업명은 "잔고 장애 중 관측
지속 및 복구 처리 — 부분 완료 단계"를 유지합니다(청산 공백 해소
완료 아님).

### 전달 파일

`0003-refactor-strategy-trailing_params-share.py.patch`,
`0004-docs-CHANGELOG-v1.7-2.patch`, 관련 파일 전체가 담긴 diff zip.
상세 배경은 프로젝트 문서 `2026-09-14-180s-gap-implementation-
directive.md`, `2026-09-14-180s-gap-step1-exit-calc-extraction.md`
참고.

---

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->

## 🔧 180초 감시 공백 대응 2단계 보완 — 공유 구간표 불변화 + REBOUND 판별 테스트 보강 (2026-09-14, GPT 재검토 반영, 매매 판단 로직 무변경)

### 배경

2단계(전략별 트레일링 설정 공유) 방향은 승인됐지만, 완료 처리 전에
두 가지를 보완하라는 지적을 받았습니다: (1) `TrailingParams.tiers`가
가변 리스트라 `frozen=True`에도 불구하고 반환값을 수정하면 클래스
상수 자체가 바뀌는 경로가 있었고, (2) `REBOUND` 관련 테스트가
실제로 `NeutralStrategy` 파라미터가 쓰였는지 구별하지 못하는
입력을 쓰고 있었습니다. 이번 커밋은 이 두 가지만 고칩니다.

### 지적 반영 — 완료 처리 전 수정 2건

**1) 공유 구간표가 외부에서 변경 가능했습니다.** `TrailingParams`는
`frozen=True`였지만 `tiers` 필드가 가리키는 리스트 자체는 여전히
가변이었고, `BreakoutStrategy`/`NeutralStrategy`의 `trailing_params()`
가 클래스 상수 리스트를 그대로(복사 없이) 반환했습니다. 직접
재현한 결과: 어떤 `BreakoutStrategy` 인스턴스에서 받은
`trailing_params().tiers[0]`을 수정하자, **다른 인스턴스**의
`generate_signal()` 결과가 평균단가 10,000원·최고가 10,600원·
현재가 10,200원 입력에서 SELL → HOLD로 바뀌었습니다 — 읽기 전용
설정 조회가 실제 운영 전략까지 바꿀 수 있는 경로였습니다.

**수정**: `TrailingParams.tiers`와 `calc_trailing_stop()`/
`_pick_trail_pct()`의 `tiers` 매개변수 타입을 `tuple[tuple[float,
float], ...]`(불변)로 바꾸고, `Breakout`/`NeutralStrategy`의 클래스
상수도 리스트 리터럴(`[...]`)에서 튜플 리터럴(`(...)`)로 바꿨습니다
(`BottomStrategy`는 매 호출 새 값을 만들어 반환해 원래 공유 문제는
없었지만, 타입 일관성을 위해 튜플로 통일). 이제 `params.tiers[0] =
...`는 `TypeError`로 즉시 막힙니다.

**2) REBOUND 판별 테스트가 실제 연결을 구별하지 못했습니다.**
기존 테스트 입력(평균단가 10,000원·최고가 10,100원·현재가=최고가)
에서는 올바른 연결(`NeutralStrategy`, 트레일링 활성이지만 미트리거
→ `None`)과 잘못된 연결(이름만 보고 `BottomStrategy`, 트레일링
시작가 미달 → `None`)이 **둘 다 `None`을 반환**해, 어느 전략
파라미터가 실제로 쓰였는지 결과로는 구별할 수 없었습니다.

**수정**: 같은 평균단가·최고가에서 현재가를 9,950원으로 바꿔,
`NeutralStrategy` 파라미터(시작 1.005배)로는 트레일링이 활성·
트리거(`TRAILING` 후보)되지만 `BottomStrategy` 파라미터(시작
1.03배)로는 시작가에도 못 미쳐 항상 `None`이 되도록 했습니다.
같은 테스트에서 `BottomStrategy`로 직접 계산한 대조군도 함께
확인해, 결과가 실제로 갈라지는지 검증합니다.

### 테스트 및 검증

`test_exit_calc_equivalence.py`에 1건을 추가했습니다(총 20건):
`test_returned_tiers_are_immutable_and_shared_safely` — 반환된
`tiers`에 항목 대입 시 `TypeError`가 발생하는지, 서로 다른
`BreakoutStrategy` 인스턴스가 트레일링 파라미터를 읽기만 한 뒤에도
여전히 정확히 같은 SELL 판정을 내는지 확인합니다. 기존 REBOUND
테스트(`test_rebound_regime_uses_router_selected_strategy_not_
bottom_by_name`)는 위 설명대로 입력을 바꿔 실제 판별력을 갖도록
보강했습니다.

수정 전 상태(리스트 공유 + 구별 안 되는 REBOUND 입력)로 되돌려
`python test_exit_calc_equivalence.py`를 실행한 결과 2건(신규
불변성 테스트 + `test_breakout_trailing_params`의 타입 불일치)이
FAIL로 표시됨을 확인한 뒤 원복했습니다. `run_regression_tests.py`
38개 중 37 PASS(이전과 동일한 `test_broker_order_status.py`
fixture 누락, 무관). `legacy_tests/test_entry_watch.py` 11/11 PASS.

### 변경하지 않은 것

손절·트레일링 임계값·구간 경계·시작 배율·평가 순서는 전혀
바꾸지 않았습니다 — 값을 담는 컨테이너 타입(list→tuple)만
바꿨습니다. 무효 입력(현재가 0, 평균단가 0인데 최고가는 양수,
NaN 등) 처리는 이번에도 다루지 않습니다 — `evaluate_exit_
candidate()`를 실제 관측 루프에 연결하는 다음 단계에서, 관측
경계에 현재가·평균단가 양수·유한값 검증과 시세 신선도 검증을
추가해 "무효 입력(평가 보류)"과 "유효한 입력인데 후보 없음
(`None`)"을 구분하기로 했습니다 — 기존 정상 전략들의 무효 입력
처리 정책 자체는 바꾸지 않습니다. 실제 주문 제출과 최고가 후보의
운영 상태 병합은 이번에도 비활성 상태입니다.

### 다음 작업

`evaluate_exit_candidate()`를 실제 잔고 장애 관측 루프에 연결하되,
연결 지점에서 입력 검증(현재가·평균단가 유효성, 시세 신선도)을
먼저 두고 "평가 보류"와 "후보 없음"을 구분해 기록합니다. 이후
구현 지시서 §2 나머지 항목은 계속 별도 커밋으로 진행합니다. 작업명은
"잔고 장애 중 관측 지속 및 복구 처리 — 부분 완료 단계"를 유지합니다.

### 전달 파일

`0005-fix-strategy-trailing_params-immutable-tiers.patch`,
`0006-docs-CHANGELOG-v1.7-3.patch`, 관련 파일 전체가 담긴 diff zip.
상세 배경은 프로젝트 문서 `2026-09-14-180s-gap-step2-trailing-
params-share.md` 참고.

---

## 🔧 180초 감시 공백 대응 3단계 — 관측 경로 연결 (2026-09-15, 매매 판단 로직 무변경)

### 배경

1~2단계로 손절·트레일링 계산이 `exit_calc.py`의 순수 함수로 추출되고
전략별 `trailing_params()`가 정상 경로·관측 경로에서 공유되도록
정리됐지만, `evaluate_exit_candidate()`는 정의와 테스트만 있을 뿐
실제 운영 경로 어디에서도 호출되지 않았습니다. 잔고 API 장애(429
등)로 `_get_balance_with_cache()`가 예외를 올리면 `run_once()`가
그 사이클을 진행하지 못하고, `app/main.py`의 `trading_loop()`는
`await asyncio.sleep(180)`으로 180초를 통째로 잤습니다 — 그동안
보유 종목의 손절·트레일링 관측이 완전히 멈췄습니다("180초 감시
공백"). 이번 커밋은 GPT 재검토가 지시한 세 가지를 연결합니다: (1)
관측 입력 검증, (2) 장애 재시도 대기 중 관측 지속(주문 제출·
`highest_price` 병합은 계속 비활성), (3) `run_once()` 통합 검증.

### 변경 내용

**1) 입력 검증 — `domain/strategy/exit_calc.py`**

`classify_exit_observation_readiness()`를 추가했습니다. 잔고 장애
관측 경로는 `evaluate_exit_candidate()`를 직접 부르기 전에 반드시
이 함수를 먼저 호출합니다. 현재가·평균단가가 양수·유한값(bool·
NaN·inf 제외)인지, 캐시된 시세 나이(`price_age_seconds`)가
`max_price_age_seconds`를 넘지 않는지를 검증하고, 빈 문자열이 아닌
사유(`invalid_current_price`/`invalid_average_price`/
`price_age_unknown`/`stale_price`)를 반환하면 "평가 보류 + 그
사유"로만 기록하고 `evaluate_exit_candidate()`를 호출하지 않습니다
— 이 함수가 반환하는 `None`("유효한 입력을 평가했지만 후보 없음")과
"평가 보류"를 절대 같은 값으로 기록하지 않습니다. 순수 계산이며
시각 조회는 호출자가 미리 계산해 넘깁니다.

**2) 장애 경로 연결 — `domain/service/trading_service.py`, `app/main.py`**

- `TradingService.observe_exit_candidates_during_outage()`: 브로커
  API를 전혀 호출하지 않고(추가 429 유발 방지), 마지막으로 성공
  조회된 `cached_balance`와 캐시된 시세·장세(`cached_market_prices`/
  `cached_regime`)만으로 보유 종목별 청산 후보를 계산·기록합니다.
  `strategy_router.select(regime)`으로 정상 경로와 동일하게 전략을
  고른 뒤 `evaluate_exit_candidate()`를 호출합니다 — regime이 아직
  캐시된 적 없으면(`UNKNOWN`) 새로 판정을 시도(=브로커 호출)하지
  않고 "평가 보류(`regime_not_cached`)"로만 기록합니다.
  `self._highest_price`는 읽기만 하고 갱신·병합하지 않습니다. 이
  메서드 자체의 예외는 종목 단위로 흡수합니다(fail-open) — 한 종목의
  관측 실패가 다른 종목 관측이나 재시도 대기를 방해하지 않습니다.
- `TradingService.wait_out_balance_outage()`: `trading_loop()`가
  기존에 하던 `await asyncio.sleep(180)`을 대체합니다. **총 대기
  시간(기본 180초)은 바꾸지 않았습니다** — 잔고 API 자체의 독립적
  지수 백오프 재설계(설계문서 v2 §3 전체, `order_attempt_seq` 등)는
  이번 범위가 아닙니다. 바뀐 것은 하나, 이 180초를 한 번에 다 자지
  않고 `balance_outage_observe_interval_seconds`(기본 15초) 간격으로
  쪼개 매 구간 시작마다 `observe_exit_candidates_during_outage()`를
  호출해 관측 기회를 유지한다는 것입니다. 잔고 API를 다시 호출하지
  않습니다(재유발 방지).
- `app/main.py`의 `trading_loop()` 429 분기가 `asyncio.sleep(180)`
  대신 `await trading_service.wait_out_balance_outage()`를 호출하도록
  바꿨습니다. 그 외 분기(429가 아닌 예외)는 그대로입니다.
- 새 관측 전용 로그 `logs/exit_candidate_outage.csv`
  (`infra/storage/logger.py`의 `ExitCandidateOutageLogger`,
  `config/settings.py`의 `StorageConfig.exit_candidate_outage_log_file`)
  — `status`(`deferred`/`no_candidate`/`STOP_LOSS`/`TRAILING`)와
  `reason`을 분리된 컬럼으로 기록해 "평가 보류"와 "후보 없음"이
  뒤섞이지 않게 했습니다. `BalanceFreshnessLogger`와 동일하게
  dedup 없는 append-only입니다(각 행이 서로 다른 시점의 관측치).
- 새 설정값 2개(`config/settings.py`의 `TradingConfig`,
  `config/settings.yaml`): `exit_candidate_outage_max_price_age_seconds`
  (기본 120초), `balance_outage_observe_interval_seconds`(기본 15초).
  둘 다 초기 설정값이며 실측 후 조정 대상으로 주석에 명시했습니다.

**3) `run_once()` 통합 검증 — `test_balance_outage_exit_observation.py`(신규)**

`trading_loop()`의 except 분기가 실제로 하는 일(429 실패 → 관측을
유지하며 대기 → 복구)을 그대로 재현하는 통합 테스트를 추가했습니다:
잔고 API가 429로 실패하도록 모킹한 상태에서 `run_once()`를 호출해
예외가 그대로 전파되는지 확인(1~2단계 회귀 유지) → `asyncio.sleep`을
모킹한 채 `wait_out_balance_outage()`를 호출해 관측이 여러 번(구간
수만큼) 기록되고 그동안 `place_order`가 전혀 호출되지 않는지 확인 →
잔고 API를 복구한 뒤 `run_once()`를 다시 호출해 정상 경로가 예외
없이 완료되고, 이 새 관측 로그에 추가 행이 쌓이지 않는지(정상 경로는
이 로거를 건드리지 않음, 중복 없음) 확인합니다.

### 테스트 및 검증

`test_balance_outage_exit_observation.py`(신규, 24건): 순수 함수
`classify_exit_observation_readiness()` 단위 테스트(유효 입력·
현재가/평균단가 무효·NaN·inf·bool·시세 없음·오래된 시세·경계값
등 11건), `observe_exit_candidates_during_outage()` 단위 테스트
(캐시 없음/로거 없음/수량 0 스킵/시세 없음/오래된 시세/regime
미확보/손절 트리거/후보 없음-평가보류 구분/주문 미제출·highest_price
불변/종목별 fail-open 9건), `wait_out_balance_outage()` 단위 테스트
(총 대기시간 보존+반복 관측, 대기 중 브로커 미호출 2건), `run_once()`
통합 테스트 2건.

전체 회귀(`run_regression_tests.py`) 39개 파일 중 38 PASS — 유일한
실패는 기존 `test_broker_order_status.py`의 주문 fixture 파일 부재로,
이번 변경과 무관합니다(이전 단계부터 동일). `legacy_tests/
test_entry_watch.py` 11/11 PASS. `test_delayed_eval_candidate_
observation.py`의 `ResourceWarning`(아래 4번 참고)도 함께 제거해
`python3 -W error::ResourceWarning`로도 경고 없이 통과합니다.

수정 전 상태로 되돌려(`no_candidate`와 `deferred`를 같은 값으로
합침) 테스트를 실행한 결과, "평가 보류"와 "후보 없음"을 구분하는
테스트가 정확히 FAIL로 표시됨을 확인한 뒤 원복했습니다.

### 4) 테스트 정리 — `test_delayed_eval_candidate_observation.py`

`csv.DictReader(open(...))`가 파일 핸들을 명시적으로 닫지 않아
`ResourceWarning`을 유발하던 6곳을 `with open(...)`으로 감싼 작은
헬퍼(`_read_csv_rows()`)로 교체했습니다. 판정 로직과 무관한 테스트
정리이며, 이 파일의 38개 테스트는 모두 그대로 통과합니다.

### 변경하지 않은 것

- 손절·트레일링·안전망·VWAP이탈·급락청산 등 기존 정상 전략의 판단
  기준·임계값·평가 순서는 전혀 바꾸지 않았습니다.
- 기존 정상 전략(`generate_signal()`)들의 무효 입력(현재가 0 등)
  처리 정책은 바꾸지 않았습니다 — `classify_exit_observation_
  readiness()`는 오직 잔고 장애 관측 경로 호출부에서만 쓰입니다.
- 장애 중 주문 제출(BUY/SELL/강제청산)은 여전히 비활성입니다 —
  `observe_exit_candidates_during_outage()`는 `place_order`를
  호출하지 않습니다.
- 장애 중 최고가 후보의 `highest_price` 운영 병합은 여전히
  비활성입니다 — 관측 메서드는 `self._highest_price`를 읽기만
  합니다.
- 잔고 API 자체의 독립적 지수 백오프 재설계(설계문서 v2 §3의
  30→60→120→180초 단계 조정, `order_attempt_seq` 기반 판정, 주문
  상태 조회/확정 분리 §1.5)는 이번 범위가 아닙니다 — 총 대기시간
  (180초)은 그대로 유지했습니다.
- 잔고 재시도 스케줄 자체(30/60/120/180초 백오프 계산)는 바꾸지
  않았습니다 — 바뀐 것은 그 대기를 "한 번에 다 자는지, 쪼개서
  관측하며 자는지"뿐입니다.

### 다음 작업

작업명 "잔고 장애 중 관측 지속 및 복구 처리 — 부분 완료 단계"를
유지합니다. 구현 지시서 §2 나머지 항목(잔고 API 독립 지수 백오프,
주문 상태 조회/확정 분리, `order_attempt_seq` 기반 SELL 수량 신뢰
게이트, 복구 시 최고가 후보 entry_time 기반 병합)은 별도 승인 후
계속 진행합니다. 이번 단계는 "계산·기록"까지이며, 실제 청산 대응
능력(자동 제출 활성화)은 포함하지 않습니다.

### 전달 파일

`0007-feat-exit-candidate-outage-observation.patch`,
`0008-docs-CHANGELOG-v1.7-4.patch`, 관련 파일 전체가 담긴 diff zip.

---

## 🔧 180초 감시 공백 대응 3단계 보완 — GPT 재검토 5가지 지적 반영 (2026-09-15, 매매 판단 로직 무변경)

### 배경

바로 위 3단계 최초 배치본을 GPT가 재검토한 결과, 테스트는 통과했지만
실제로는 "잔고 장애 중 새로운 시세 관측"이 아니라 "180초 동안 기존
캐시를 반복 평가"하는 구현으로 범위가 바뀌어 있었습니다. 재현된 5가지
결함: (1) 대기 중 실제 시세 API 호출이 0건 — 매 구간 같은 캐시값만
재평가, (2) 주문 접수 직후 `cached_balance`가 `None`으로 비워지면
관측 기록이 통째로 0건, (3) `price_age_seconds`에 음수·`-inf`·`bool`이
들어와도 "유효"로 통과, (4) 통합 테스트가 sleep을 무력화만 하고 시간·
가격 변화를 전혀 검증 못 함, (5) 새 CSV가 daily bundle export 목록에
빠짐. "이번에는 테스트 통과와 요구사항 충족을 구분해야 한다"는 지적에
따라 다섯 가지를 모두 프로덕션 코드와 테스트 양쪽에서 고쳤습니다.

### 변경 내용

**1) 잔고 재시도 스케줄과 시세 관측 분리 — `domain/service/trading_service.py`**

`_observe_exit_candidate_for_symbol()`이 캐시를 그냥 읽지 않고
기존 `_get_market_price_with_cache()`를 그대로 재사용하도록
바꿨습니다. 이 메서드는 잔고 API와 완전히 독립된 자체 실패·429
처리를 이미 갖고 있어(주기 경과 시 실제 조회, 실패 시 캐시 폴백 +
경고 로그, 캐시조차 없으면 예외) 새 코드 없이 "장애 중에도 시세는
계속 갱신을 시도한다"는 요구사항을 만족합니다. 조회 자체가 실패하고
대체할 캐시도 없으면 새 사유 `price_fetch_failed`로 평가 보류
처리합니다. 잔고 API(`get_account_balance`)는 여전히 대기 중 한 번도
재호출하지 않습니다(429 재유발 방지, 기존 원칙 유지).

**2) 주문 접수 직후 캐시 무효화와 무관한 관측 스냅샷 — 동일 파일**

`self.cached_balance = None`은 매수/매도 주문 접수 시 기존 정책대로
계속 발생합니다(다음 폴링에 최신 잔고를 강제하기 위함, 변경 없음).
새로 추가한 `self._last_observed_positions`는 `_get_balance_with_
cache()`의 세 성공 경로 모두에서 `_update_last_observed_positions()`
로 채워지는 별도의 "관측 전용" 스냅샷으로, 저 무효화의 영향을 받지
않습니다. 감시 대상 종목도 이 스냅샷뿐 아니라 `entry_time_by_symbol`
/`unresolved_order_intents`/`PositionStateMachine`의 OPEN·
BUY_PENDING·SELL_PENDING 상태까지 합쳐 `_symbols_needing_outage_
observation()`으로 넓혔습니다. 스냅샷에 없는(평단을 모르는) 종목은
계산을 지어내지 않고 새 사유 `position_unconfirmed`로만 기록합니다.
이 스냅샷은 체결 확정이나 수량 판정에는 전혀 쓰이지 않습니다 — 그러면
2026-09-14에 이미 되돌렸던 "스테일 수량이 체결 확인으로 오인되는"
위험을 다시 들여오게 되기 때문입니다.

**3) 음수·무한대·bool 시세 나이 검증 — `domain/strategy/exit_calc.py`**

`classify_exit_observation_readiness()`의 나이 검증을 `_is_valid_age()`
로 강화해, 시스템 시계가 뒤로 보정될 때(NTP 등) 발생할 수 있는 음수
나이와 `-inf`, `bool`을 새 사유 `invalid_price_age`로 명시 거부합니다
(재현: 기존 구현은 `-60`/`-inf`/`True`가 모두 "유효"를 반환했습니다).
이 검증을 엄격히 만드는 과정에서, `_observe_exit_candidate_for_
symbol()`이 시세 조회 **전**에 미리 떠 둔 시각을 나이 계산에 그대로
쓰다가 "방금 새로 조회에 성공한" 케이스에서 아주 작은 음수 나이가
나오는 실제 버그도 함께 발견해 고쳤습니다(나이는 반드시 조회 성공
**이후** 시각을 기준으로 재계산).

**4) daily bundle export에 새 CSV 연결 — `export_daily_bundle.py`**

`CSV_SOURCES` allowlist에 `("exit_candidate_outage.csv",
("detected_at",))`를 추가했습니다. 이전과 동일한 이유로, 여기 명시
추가하지 않으면 실시간 로그는 정상 쌓여도 daily bundle에는 실리지
않아 장애 발생일 분석에서 핵심 자료가 누락됩니다.

**5) 통합 테스트를 실제 시간 전진·가격 변화로 재작성 —
`test_balance_outage_exit_observation.py`**

`_FrozenDateTime`(`datetime.now()`만 오버라이드하는 가짜 시계)을
도입해, `asyncio.sleep` mock이 실제로 시간을 전진시키도록 했습니다.
`TestRunOnceOutageRecoveryIntegration`을 두 시나리오로 재작성했습니다:
(a) 정상 보유(실제 `_get_balance_with_cache()`/`_get_market_price_
with_cache()` 호출로 스냅샷 확보) → 잔고 429 → 대기 중 가격을 손절선
아래로 하락 → `price_refresh_seconds` 경과 후 세 번째 관측에서 실제로
새 가격(하락분)을 포착해 STOP_LOSS로 기록 → 대기 중 주문 0건 →
잔고 복구 → 복구된 정상 경로가 미뤄졌던 손절을 **정확히 한 번**
매도로 처리(그 결과 `cached_balance`가 기존 정책대로 다시 비워짐)
까지 하나의 `place_order` 추적 mock으로 전체를 감싸 검증합니다.
(b) 주문 접수 직후 캐시가 비어도 스냅샷·로컬 상태가 있으면 관측이
0건이 되지 않는다는 별도 시나리오. 기존 23개 테스트 중 8개는 새
동작(적극적 시세 재조회, 스냅샷 기반 감시 대상 결정)에 맞게 함께
고쳤고, `invalid_price_age`/`price_fetch_failed`/`position_unconfirmed`
각각의 전용 회귀 테스트를 새로 추가했습니다. `export_daily_bundle.py`
의 CSV 연결도 `test_shadow_analysis.py`에 end-to-end 테스트(X절)로
확인합니다.

### 검증

- `test_balance_outage_exit_observation.py`: 31/31 통과(신규 7건 포함).
- `test_shadow_analysis.py`: 189/189 통과(신규 4건 포함, X절).
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패
  (`test_broker_order_status.py`)는 이번 변경과 무관한 기존 fixture
  경로 누락(이 스크래치 클론에 `tests/fixtures/order_reconciliation/`
  디렉터리 자체가 없음)입니다.
- `legacy_tests/test_entry_watch.py`: 11/11 통과.
- 의도적 결함 주입 검증: (1) 시세를 능동 재조회하지 않고 캐시만 읽게
  되돌리면 관련 테스트 2건이 즉시 실패, (2) 감시 대상 결정을 다시
  `cached_balance` 하나로 좁히면 관련 테스트 9건이 즉시 실패 — 두
  경우 모두 원복 후 재확인해 정상 통과.

### 전달 파일

`0005`~`0011` 패치(파일별 세분화 커밋 7개 — exit_calc.py 나이 검증,
trading_service.py 관측 연결 본체, logger.py 주석, export_daily_
bundle.py CSV 연결, 테스트 2건, 이 CHANGELOG), 관련 파일 전체가
담긴 diff zip.

---

## 🔧 180초 감시 공백 대응 3단계 2차 보완 — GPT 3차 재검토 3가지 지적 반영 (2026-09-15, 매매 판단 로직 무변경)

### 배경

바로 위 2차 배치(패치 `0005`~`0012`)를 GPT가 세 번째로 재검토했습니다.
이번엔 "완료 보류" 대신 "핵심 결함은 상당 부분 해결됐으나, 다음 단계로
넘어가기 전에 다음 세 가지는 보완하는 게 좋겠다"는 판정이었습니다 —
이 중 1·2번은 "우선 수정"(재현 가능한 실제 결함), 3번은 "검증
보완"(테스트 문구가 실제 검증 범위보다 넓었던 것)으로 구분해서
전달받았습니다.

1. **[우선 수정] 시세 조회도 429가 발생하면 계속 재호출** — 관측이
   재사용하는 `_get_market_price_with_cache()`는 시세 조회 실패에
   대한 별도 재시도 시각 관리가 없어서, 갱신 주기를 넘긴 캐시는
   실패 후에도 계속 만료 상태로 남아 다음 관측(예: 15초 뒤)에서 바로
   다시 조회를 시도했습니다. 재현: 시세 나이 60/75/90초 세 번 모두
   재호출하며 매번 429. "잔고 API와 시세 API가 독립된 호출 한도를
   갖는다"는 이전 주석의 주장도 확인된 근거가 없어 삭제 대상으로
   지적됐습니다.
2. **[우선 수정] 관측이 느리면 실제 대기시간이 설정값보다 길어짐** —
   `wait_out_balance_outage()`가 `asyncio.sleep()`한 시간만 `elapsed`
   에 더하고, 매 구간 시작마다 부르는 동기식 관측(`observe_exit_
   candidates_during_outage()`) 자체의 소요 시간은 전혀 계산하지
   않았습니다. 재현: 관측 1회에 20초가 걸리도록 만들면, 설정 45초/
   간격 15초에서 실제 경과가 105초까지 불어남.
3. **[검증 보완] 주문 제출 1회는 체결 부수 효과 1회를 증명하지
   않음** — 3단계 통합 테스트가 "복구 부수 효과의 정확히 1회 적용"을
   증명했다고 말하는 주석은 실제로는 "복구 직후 SELL 제출 1회
   확인"까지만 검증한 것이었습니다. 그 이후 체결 확인 과정의 손익·
   수량 반영, 부분체결/최종체결 처리, 같은 체결 증거가 반복 노출됐을
   때 중복 반영 방지, 다음 폴링에서 중복 SELL이 없는지는 별도로
   검증돼야 한다는 지적입니다.

GPT는 "장애 중 주문 제출"과 "운영 최고가 병합 후보"는 계속 비활성으로
유지하고, 이번 라운드의 상태는 "장애 중 관측 보완"이며 실제 청산
불가와 잔고 재시도 구조 개선은 여전히 별도 미해결로 남겨 달라고
명시했습니다 — 이번 배치도 그 범위를 그대로 지킵니다.

### 변경 내용

**1) 시세 조회 실패 백오프 + `price_source` 구분 —
`domain/service/trading_service.py`, `config/settings.py`/
`settings.yaml`, `infra/storage/logger.py`**

`_get_market_price_with_cache()`에 종목별 실패 시각(`self._market_
price_fetch_failed_at`)을 새로 기억시켜, 새 설정
`market_price_retry_backoff_seconds`(기본 60초, `price_refresh_
seconds`와 동일한 기본값 — 실패 감지 이후에만 추가로 억제) 동안은
재조회 주기가 지났어도 재시도 자체를 건너뛰고 기존 캐시를 그대로
씁니다. 이 백오프는 이 메서드를 부르는 모든 경로(장애 관측·정상
순회 공통)에 적용됩니다. "잔고 API와 시세 API가 독립된 호출 한도를
갖는다"던 확인되지 않은 주장은 관련 주석 두 곳에서 모두 삭제했습니다.

호출자가 "새로 조회했는지 / 정상 캐시 재사용인지 / 갱신 실패 후
대체인지 / 아예 조회 불가인지"를 구분할 수 있도록 `self._market_
price_fetch_outcome[symbol]`(`fetched`/`cache_fresh`/`cache_after_
failure`/`unavailable`)을 함께 기록하고, `_observe_exit_candidate_
for_symbol()`이 이 값을 관측 CSV의 새 컬럼 `price_source`로 그대로
옮겨 적습니다 — 이전에는 "정상 캐시 재사용"과 "갱신 실패 후 대체"가
같은 `current_price`로만 보여 CSV만으로 구분이 불가능했습니다.

**2) `wait_out_balance_outage()`의 monotonic 예산 관리 — 동일 파일**

각 관측 호출 앞뒤로 실제 벽시계(`self._monotonic`, 기본은 `time.
monotonic` — 전역이 아니라 인스턴스 속성으로 감싸, asyncio 이벤트
루프 내부의 스케줄링 호출까지 테스트의 결정적 시퀀스에 휘말리는 것을
피했습니다)를 읽어 그 소요 시간을 `elapsed`에 더하고, 다음 sleep
구간(`chunk`)은 남은 예산(`total_seconds - elapsed`)에서 계산합니다.
관측이 느려질수록 다음 sleep이 그만큼 줄어들어, 관측 소요시간이
총 대기 위에 고스란히 얹히지 않습니다. 다만 이미 시작된 동기식 API
호출 자체를 중간에 취소할 수는 없으므로, "정확히 `total_seconds` 안에
끝난다"고 보장하지는 않습니다 — 무한정 계속 불어나는 문제만 없앱니다.
잔고 API 자체의 30/60/120/180초 백오프 재설계는 여전히 이번 범위
밖입니다(변경 없음).

**3) 통합 테스트 주장 범위 축소 + 반복 폴링 중복 방지 검증 추가 —
`test_balance_outage_exit_observation.py`**

`TestRunOnceOutageRecoveryIntegration`의 클래스 독스트링과
`test_429_then_price_drop_during_wait_then_recovers_cleanly`의 인라인
주석에서 "복구 부수 효과의 정확히 1회 적용을 증명했다"는 표현을
"복구 직후 SELL 제출 1회 확인까지만 증명한다"로 좁혔습니다(MockBroker
는 `place_order()` 안에서 즉시·완전 체결시키므로 "체결 확인"이라는
별도 단계 자체가 없다는 점도 명시). 새 테스트
`test_recovery_sell_does_not_repeat_on_subsequent_polls`를 추가해,
이미 손절선 아래인 종목을 복구 경로로 한 번 청산시킨 뒤 `run_once()`
를 두 번 더 호출해도 같은 SELL이 중복 제출되거나 거래 로그
(`trade_log.csv`)에 중복 기록되지 않음을 확인합니다(프로덕션 코드는
바꾸지 않았습니다 — 검증 공백을 메우는 테스트만 추가).

### 검증

- `test_balance_outage_exit_observation.py`: 37/37 통과(신규 8건:
  백오프 4건, 느린 관측 예산 1건, 반복 폴링 중복방지 1건, `price_
  source` 구분 1건 — 그리고 기존 `test_total_wait_time_unchanged_
  and_observes_repeatedly`를 새 monotonic 계약에 맞게 결정적으로
  갱신).
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패
  (`test_broker_order_status.py`)는 이전 두 라운드와 동일하게 이번
  변경과 무관한 기존 fixture 경로 누락(`tests/fixtures/order_
  reconciliation/`)입니다.
- `legacy_tests/test_entry_watch.py`: 11/11 통과.
- 의도적 결함 주입 검증(둘 다 원복 후 재확인 완료): (1) 백오프
  판정(`in_backoff`)을 항상 `False`로 되돌리면
  `TestMarketPriceFetchBackoff`의 2건이 즉시 실패(재조회를 억제하지
  못함), (2) `wait_out_balance_outage()`를 관측 소요시간을 계산에
  넣지 않던 이전 구현으로 되돌리면
  `test_slow_observation_shrinks_next_sleep_so_total_wait_does_not_
  inflate`가 즉시 실패(sleep 3회로 예산 초과).

### 전달 파일

`0013`~`0018` 패치(파일별 세분화 커밋 6개 — settings.py/yaml 새 설정,
trading_service.py 백오프+monotonic 예산 본체, logger.py 필드 주석,
테스트 파일, 이 CHANGELOG), 관련 파일 전체가 담긴 diff zip.

---

## 🔧 180초 감시 공백 대응 3단계 3차 보완 — GPT 4차 재검토 3가지 지적 반영 (2026-09-15, 매매 판단 로직 무변경)

### 배경

위 2차 보완(패치 0013~0018)을 GPT가 4차 재검토했습니다. 회귀
테스트는 모두 통과(37/37, 38/39, 11/11)했지만, 그중 2건은 "테스트
통과"와 "요구사항 충족"이 여전히 다르다는 이 프로젝트의 표준
교훈대로 실제로는 완전히 고쳐지지 않은 상태였습니다. GPT는 이번
보완을 정확히 세 항목으로 제한할 것을 명시했고(장애 중 주문 제출·
운영 최고가 병합 비활성화 유지, `price_source`·기존 관측 안전장치
보존), 완료 후에는 이번 관측 공백 대응 작업 자체를 닫고 다음
라운드(독립 잔고 재시도, 실제 청산 가능성)로 넘어가는 것을
제안했습니다.

### 변경 내용

**1) `wait_out_balance_outage()`를 절대 monotonic 데드라인 기준으로
재작성 — "우선 수정" (`domain/service/trading_service.py`)**

2차 보완은 관측 호출 소요 시간을 `elapsed`에 더했지만, 여전히
`elapsed += chunk`로 **요청한** sleep 시간만 누적했습니다 —
`asyncio.sleep()` 자체가 이벤트 루프 지연 등으로 예정보다 늦게
복귀하는 경우는 반영하지 못했습니다(GPT 재현: 45초 예산/15초 간격,
매 sleep이 예정보다 20초씩 늦게 복귀하면 실제 경과가 105초까지
불어나고, 이미 마감(45초)을 넘긴 70초 시점에도 관측을 새로 시작).
이제 시작 시 절대 마감(`deadline = self._monotonic() + total_
seconds`)을 한 번만 계산하고, 반복마다 관측 직후 `deadline - self.
_monotonic()`으로 남은 시간을 다시 계산합니다 — `elapsed` 누적
변수 자체를 제거해 "요청한 시간"이 아니라 "실제로 흐른 시간"만
기준으로 삼습니다. 마감을 넘긴 뒤에는 새 관측이나 추가 sleep을
시작하지 않습니다.

**2) 시세 재조회 백오프를 "실패를 잡은 시점"의 monotonic 기준으로
예약 — "우선 수정" (동일 파일)**

2차 보완의 백오프 판정은 `datetime.now()`(시스템 시계)와 "호출을
시작한 시점"을 썼는데 둘 다 결함이 있었습니다: (1) 시스템 시계가
NTP 등으로 보정되면 실제 경과 시간과 무관하게 백오프가 어긋나고,
(2) 조회 자체가 느리게(예: 10초) 실패하면 "호출 시작 시점"을 실패
시각으로 기록해 그만큼 백오프가 짧아집니다(GPT 재현: 10초 걸려
실패 → 그 실패 이후 50초만 지나도 재시도 허용, 설정 60초보다
짧음). 이제 실패를 "잡은 시점"(느린 호출이 실제로 끝난 뒤)에
`self._monotonic()` 기준 절대 재시도 허용 시각
(`_market_price_fetch_next_retry_at[symbol]`)을 예약하고, 백오프
판정도 그 monotonic 값으로만 합니다. 사람이 읽는 실패 시각
(`_market_price_fetch_failed_at`, datetime)은 로그용으로만 남기고
게이트 판단에는 쓰지 않습니다.

GPT가 지적한 문서 정확성도 함께 반영했습니다: 이 백오프는
`_get_market_price_with_cache()`를 호출하는 모든 경로(장애 관측 +
보유 종목 정상 순회 중 이 메서드를 거치는 경로)에 영향을 주므로
"순수 관측 전용 변경"은 아닙니다. 다만 정상 순회 경로 중 일부는 이
메서드를 거치지 않고 `broker.get_market_price()`를 직접
호출하므로(실패 시에만 이 메서드로 폴백) "모든 시세 호출을 공통
제어한다"도 아닙니다 — 이 백오프가 실제로 통제하는 범위는 이
메서드 자신의 호출 경로로 한정됩니다.

**3) 반복 폴링 검증을 실제 429→관측→복구 시나리오에 연결 — 검증
보완, 프로덕션 코드 무변경 (`test_balance_outage_exit_observation.py`)**

2차 보완이 추가한
`test_recovery_sell_does_not_repeat_on_subsequent_polls`는 실제
429→관측→복구 경로를 거치지 않고 손절 조건을 직접 만들어
`run_once()`를 반복 호출했을 뿐이었습니다(재현 시나리오와 무관).
이 검증을 `test_429_then_price_drop_during_wait_then_recovers_
cleanly`의 복구 직후로 옮겨, 실제 429→관측→복구를 거친 뒤 후속
폴링(2회)에서도 중복 SELL·중복 거래 로그가 없는지 확인하도록
바꿨습니다. 검증 범위는 "SELL 제출·접수 로그 중복 없음"까지로
명확히 제한했습니다 — 손익·수량이 그 SELL을 통해 정확히 한 번만
반영됐는지는 관련 상태값(예: 실현손익 누계)을 직접 확인하지
않았으므로 이 테스트로 주장하지 않습니다. 기존 별도 테스트는
중복이므로 제거했습니다.

### 검증

- `test_balance_outage_exit_observation.py`: 39/39 통과(신규 3건 —
  `wait_out_balance_outage` 지연 복귀 재현 1건, 백오프 느린
  실패·시스템 시계 점프 재현 2건 — 그리고 기존
  `TestWaitOutBalanceOutage` 3건·`TestMarketPriceFetchBackoff` 4건을
  가변 가짜 monotonic 시계로 재작성).
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패
  (`test_broker_order_status.py`)는 이전 라운드들과 동일하게 이번
  변경과 무관한 기존 fixture 경로 누락(`tests/fixtures/order_
  reconciliation/`)입니다.
- `legacy_tests/test_entry_watch.py`: 11/11 통과.
- 의도적 결함 주입 검증(모두 원복 후 재확인 완료): (1)
  `wait_out_balance_outage()`를 이전 `elapsed += chunk` 방식으로
  되돌리면 지연-복귀 재현 테스트와 기존 관측-예산 테스트 2건이 즉시
  실패, (2) 백오프 판정을 `datetime.now()` 기준으로 되돌리면 느린
  실패·시스템 시계 점프 재현 테스트 2건이 즉시 실패, (3) MockBroker의
  포지션 제거 로직을 임시로 무력화하면(완전 체결에도 포지션이 안
  지워짐) 확장된 통합 테스트의 "복구 후에도 포지션이 완전히
  청산된 채 유지" 확인이 즉시 실패.

### 상태 (2026-09-15, GPT 4차 재검토 재확인 후 정정)

0018~0021을 독립 클론에서 재검증한 뒤 GPT가 최종 확인했습니다 — 이번
관측 보완 범위(장애 중 시세·청산 후보 관측)는 **개발 검증
완료·운영 확인 대기**로 기록합니다. "180초 감시 공백 전체 해결"은
과장된 표현이었으므로 정정합니다 — 아직 남은 항목:

- 실제 운영에서 로그·재시도 동작 확인 — 운영 확인 대기.
- 잔고 API의 독립 재시도 구조 — 미해결.
- 잔고 장애 중 실제 청산 가능성 — 미해결.
- 동기식 API·순차 처리로 인한 감시 지연 — 미해결.

### 다음 작업

관측 기능을 계속 보완하기보다, **잔고 재시도를 전체 매매 루프
대기에서 분리하는 설계**로 넘어갑니다 — 정상 잔고를 다시 확보하면
180초 대기 완료를 기다리지 않고 즉시 정상 경로로 복귀하는 것이
목표입니다. 잔고 재시도·시세 관측·주문 상태 대조 세 축의 실행
시점을 분리하는 설계 문서를 별도로 작성합니다(장애 중 주문 미제출
원칙은 유지, 실제 청산 허용은 별도 정책 검토로 남김). 아직 코드
변경 없음 — 설계 단계.

### 전달 파일

패치 3개(파일별 세분화 커밋 — `trading_service.py` 데드라인 재작성,
`trading_service.py` 백오프 monotonic 전환, 테스트 파일 갱신) + 이
CHANGELOG, 실제 변경된 파일만 원래 폴더 경로 그대로 담은 diff zip.

---

## 🔧 독립 잔고 재시도 — 잔고 재시도를 전체 매매 루프 대기에서 분리 (2026-09-15, GPT 6차 재검토 반영, 매매 판단 로직 무변경)

### 배경

앞선 CHANGELOG 정정에서 예고한 대로, 관측 기능 보완 대신 **잔고
재시도를 전체 매매 루프 대기에서 분리**하는 설계로 넘어갔습니다.
1차 설계안(문서 `2026-09-15-independent-balance-retry-design.md`
초안)을 GPT에게 제출했으나, 재검토에서 두 가지 실질적 결함과 한
가지 위험을 지적받아 설계를 수정한 뒤 구현했습니다.

- **결함 1 (캐시 재사용 실패)**: "복구된 잔고를 캐시에 넣어두면
  다음 `run_once()`가 재사용할 것"이라는 가정 — `_get_balance_
  with_cache()`는 미해결 주문이 있으면(`_has_unresolved_orders()`)
  캐시 나이와 무관하게 항상 강제 재조회하므로, 이 가정은 흔한
  상황(미해결 주문 존재)에서 성립하지 않습니다.
- **결함 2 (무기한 대기 루프)**: 총 대기 예산(`total_seconds`)을
  없애고 잔고가 복구될 때까지 함수 내부에서 무기한 도는 구조를
  제안했으나, 이는 그 함수가 반환하기 전까지 `trading_loop()`의
  장 마감·날짜변경 감지로 돌아갈 수 없다는 뜻입니다 — 구
  `wait_out_balance_outage()`(180초 블로킹)와 같은 종류의 문제를
  무기한으로 확장한 것에 불과합니다.
- **위험 지적**: 예외 메시지의 `"http=429"` 문자열만으로 잔고
  장애를 판정하면, 시세·주문상태 등 다른 API에서 난 429까지
  잔고 장애로 오인해 불필요한 잔고 재조회를 유발할 수 있습니다.

GPT는 이번 구현 범위를 "잔고 재시도 분리 + 복구 응답의 정상 처리
연결"로 명시적으로 한정하고, 6개 항목의 구현 지시와 필수 테스트
목록을 함께 제시했습니다.

### 변경 내용

1. **`wait_out_balance_outage()` 완전 제거**, 대신 4개 메서드로
   대체(`domain/service/trading_service.py`):
   - `enter_balance_outage()` — 멱등. 재시도 타이머(30초)와 관측
     타이머(즉시)를 세팅하고 즉시 반환.
   - `is_in_balance_outage()` — 상태 조회.
   - `handle_balance_outage_tick()` (async) — 매 폴링마다 짧게
     호출. 이번 tick에 재시도 시각이 됐으면 재시도 1회(우선),
     아니면 관측 시각이 됐으면 관측 1회만 수행하고 반환(둘 다
     아니면 API를 아예 부르지 않고 즉시 반환) — 없앤 것은 "내부에서
     여러 번 반복하며 총 180초를 도는 루프"이지, 호출 자체의 소요
     시간을 보장하지는 않는다(정정: 2026-09-16 GPT 7차 재검토 지적,
     아래 참고). 매 tick 첫머리에 `_check_and_handle_daily_reset()`을
     호출해 장애 중에도 날짜변경을 놓치지 않음.
   - `_exit_balance_outage()` — 복구 시 상태 초기화(다음 장애는
     다시 30초부터).
2. **`app/main.py`의 `trading_loop()`**: 장애 중에는 `run_once()`
   대신 `handle_balance_outage_tick()`을 호출하도록 분기 — while
   루프 자체는 평소 poll 주기 그대로 계속 돌아, 장애 중에도 장
   마감·날짜변경·취소(`CancelledError`) 검사를 정상적으로 통과.
   예외 처리도 문자열 매칭(`"http=429" in str(exc)`) 대신 `_get_
   balance_with_cache()`가 실패 시 표시해 두는 `kiwoom_balance_
   fetch_failure` 예외 속성으로 판정해, 다른 API의 429를 잔고
   장애로 오인하지 않도록 함(결함 3 대응).
3. **복구 응답의 정상 처리 연결(결함 1 대응)**: `run_once()`를
   `_check_and_handle_daily_reset()` → `_get_balance_with_cache()`
   → `_run_once_with_balance(balance)`로 분리(순수 추출). 잔고
   재시도가 성공하면 그 응답을 캐시에 넣는 것으로 끝내지 않고,
   같은 tick 안에서 `_run_once_with_balance(balance)`를 직접
   호출해 정상 전략 처리로 **한 번만** 흘려보냄 — 불필요한 잔고
   재조회 없이, 기존 미해결 주문 강제 재조회 안전장치는 그 외
   상황에서 그대로 유지.
4. **잔고 재시도 백오프**: monotonic 절대시각 기반 지수 백오프,
   30→60→120→180초 상한, 성공 시 다음 장애부터 30초로 리셋. 새
   설정값 `balance_retry_backoff_min_seconds`(기본 30)/`balance_
   retry_backoff_max_seconds`(기본 180)를 `config/settings.py`
   (양수·유한·min≤max 검증 포함)/`settings.yaml`에 추가.
5. **이번 범위에서 제외**(GPT 지시 5번): 장애 중 주문 상태 조회,
   장애 중 주문 제출, `highest_price` 운영 병합 — 모두 계속
   비활성/미구현.

### 검증

- `test_balance_outage_exit_observation.py`: 45/45 통과 — 구
  `TestWaitOutBalanceOutage`(4건)를 삭제하고 새 `TestBalanceOutageRetry`
  (10건: 초기 백오프·관측 예약, 멱등성, 재시도 전 tick 무동작,
  관측·재시도 독립 주기, 반복 실패 백오프 30→60→120→180 유지,
  429 아닌 실패 시 재예외, 미해결 주문 있는 복구 성공 시 잔고 API
  정확히 1회 호출, 복구 로그의 trigger_reason, 복구 후 백오프
  리셋, 장애 중 날짜변경 감지)로 교체. 기존 통합 테스트(`test_
  429_then_price_drop_during_wait_then_recovers_cleanly`)는 tick
  기반 시뮬레이션(t=0,15,30,45,60,75,90)으로 재작성해 유지.
- `test_review_safety_regressions.py`: 28/28 통과 — `trading_loop()`
  레벨 신규 3건(잔고 API의 429는 장애 진입/다른 API의 429는 미진입
  대응쌍, 장애 중에도 장 마감 감지가 막히지 않음).
- `test_balance_429_fallback.py`, `test_balance_freshness_observation.py`:
  16/16 통과(`_get_balance_with_cache()` 리팩터링이 기존 동작을
  보존하는지 확인).
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패
  (`test_broker_order_status.py`)는 이전 라운드들과 동일하게 이번
  변경과 무관한 기존 fixture 경로 누락입니다.
- `legacy_tests/test_entry_watch.py`: 11/11 통과.
- 의도적 결함 주입 검증(모두 원복 후 재확인 완료): (1)
  `enter_balance_outage()`의 멱등성 가드를 제거하면 타이머 리셋
  테스트가 즉시 실패, (2) 백오프 상한(`min(...)`)을 제거하면 반복
  실패 백오프 테스트가 즉시 실패(240초로 계속 증가 확인), (3)
  복구 시 `_run_once_with_balance(balance)` 호출을 `run_once()`
  재호출로 되돌리면 "미해결 주문 있는 복구 성공" 테스트가 잔고
  API 2회 호출로 즉시 실패, (4) `trading_loop()`의 예외 판정을
  `"http=429" in str(exc)` 문자열 매칭으로 되돌리면 "다른 API의
  429는 미진입" 테스트가 즉시 실패.

### 상태

이번 라운드로 GPT 6차 재검토가 지시한 6개 항목(다른 API 429 구분,
짧게 반환하는 tick 기반 처리, 복구 응답의 정상 처리 연결, PSM
대조·차단 조건 유지 및 부수효과 중복 방지, 장애 중 주문상태
조회·주문제출·highest_price 병합 계속 비활성, 6개 필수 테스트)를
모두 반영했습니다. 남은 항목(이전 CHANGELOG 정정과 동일):

- 실제 운영에서 로그·재시도 동작 확인 — 운영 확인 대기.
- 장애 중 주문 상태 조회-전용 대조 — 이번 범위 제외, 별도 후속
  작업.
- 잔고 장애 중 실제 청산 가능성 — 별도 정책 검토 대상, 미해결.

### 전달 파일

패치(파일별 세분화 커밋 — `config/settings.py`·`settings.yaml`
설정값 추가, `domain/service/trading_service.py` 리팩터링 및 신규
메서드, `app/main.py` `trading_loop()` 분기 변경, 테스트 파일 2개
갱신, 이 CHANGELOG) + 실제 변경된 파일만 원래 폴더 경로 그대로
담은 diff zip.

---

## 🔧 독립 잔고 재시도 보완 — GPT 7차 재검토 3가지 지적 반영 (2026-09-16, 매매 판단 로직 무변경)

### 배경

GPT가 0023~0027 패치를 이전 검증본에 적용해 직접 실행한 결과, 잔고
장애 tick 자체의 회귀 테스트(45/45, 28/28, 11/11, 전체 38/39)는
모두 유효했지만, 장중 경로에만 집중하느라 놓친 문제 2건과 설명
오류 1건을 지적받았습니다. 첫 전달 시 zip 파일명 관련 혼선이
있었으나, 실제 전달된 `kiwoom_auto_trader_round5_changed_files_
20260916.zip`은 패치 적용 결과와 파일별 해시가 모두 일치함을
재확인했습니다(문제 없음, 재포장 불필요).

### 변경 내용

1. **장외 시간에 잔고 백오프가 우회되던 문제(높음)**: `trading_
   loop()`의 장외(after-hours) 분기가 `reconcile_after_market_
   close()`를 직접 호출해, 미해결 주문이 있으면 매 폴링(기본
   10초)마다 실제 잔고 API를 그대로 호출했습니다 — 장중 경로에만
   새 백오프·outage 상태를 적용했던 게 원인입니다. 이제 장외
   분기도 `is_in_balance_outage()`를 확인해 이미 장애 상태면
   `handle_balance_outage_tick(reconcile_only=True)`에 위임하고,
   그 호출이 실패하면(429 등) 장중과 동일하게 `enter_balance_
   outage()`로 전환합니다 — 장중·장외 공통으로 30→60→120→180초
   백오프가 적용됩니다.
2. **복구 후 처리 구분(장외에는 대조·저장만)**: 장외 시간에 잔고가
   복구돼도 정상 전략 처리(`_run_once_with_balance()`, 신규 주문
   제출 가능)로 이어지면 안 되므로, `handle_balance_outage_tick()`
   에 `reconcile_only` 파라미터를 추가했습니다 — 참이면 복구 성공
   시 `reconcile_after_market_close()`와 동일하게 PSM 대조·상태
   저장만 수행합니다(주문 제출 없음).
3. **장외 연속 실패가 마감 리포트·날짜변경 확인을 막던 문제
   (높음)**: 개정 전에는 장외 대조 호출의 예외가 그대로 `trading_
   loop()`의 try 블록을 빠져나가, 같은 폴링 안의 날짜변경 확인·
   마감 리포트 생성 코드에 전혀 도달하지 못했습니다(GPT 실측
   재현: 연속 429 중 마감 리포트 호출 0회, 날짜변경 검사 0회).
   이제 장외 분기의 대조 호출을 자체 try/except로 감싸 실패를
   흡수하고, 날짜변경 확인(`_check_and_handle_daily_reset()`)은
   장 상태·대조 성공 여부와 무관하게 매 폴링 항상 실행됩니다.
   마감 리포트도 대조 실패 때문에 건너뛰지 않되, 리포트 생성
   시점에도 미해결 주문이 남아 있으면 `_run_end_of_day_tasks()`가
   "대조 미완료" 경고를 별도로 남겨(손익·포지션 수치가 최신
   확정 상태가 아닐 수 있음을 알림) 무조건 정상 완료로 보이지
   않게 했습니다.
4. **다른 API의 429가 180초→10초로 짧아진 회귀(높음)**: 라운드5
   변경으로 잔고 조회가 아닌 다른 API(시세·주문상태 등)의 429는
   `kiwoom_balance_fetch_failure` 태그가 없어 일반 예외와 함께
   poll 주기(10초)만 쉬고 바로 재시도됐습니다 — 개정 전(라운드4
   까지)에는 이런 예외도 180초를 통째로 블로킹해 최소한의 재진입
   제한이 있었는데, 그 보호가 조용히 사라진 회귀였습니다. `app/
   main.py`에 독립된 monotonic 쿨다운(`OTHER_API_RATE_LIMIT_
   COOLDOWN_SECONDS`=180초, 잔고 장애 상태와 별개)을 추가해, 다른
   API의 429는 잔고 장애로 오인하지 않으면서도 180초 동안 정상
   순회(`run_once()`)를 건너뛰도록 복원했습니다 — 단, 예전처럼
   블로킹하지 않고 매 poll마다 짧게 확인하는 방식이라 장 종료·
   날짜변경·취소 감지는 쿨다운 중에도 그대로 동작합니다.
5. **설명 정정("tick은 수 초 이내 반환 보장" 삭제)**: `handle_
   balance_outage_tick()`이 없앤 것은 "내부에서 여러 번 반복하며
   총 180초를 도는 루프"뿐입니다 — 잔고 재시도 자체(동기식 HTTP
   호출)와, 복구 성공 시 같은 tick 안에서 실행되는 `_run_once_
   with_balance()`(종목별 동기식 API 호출 순차 수행)의 소요 시간은
   여전히 남아 있습니다. "절대 여러 초 이상 블로킹하지 않는다"는
   기존 설명(트레이딩 서비스 독스트링, `app/main.py` 주석, 테스트
   docstring, 이 CHANGELOG 이전 항목)을 모두 정정하고, 동기식
   호출 지연 자체를 없애는 것(비동기 재설계)은 이번 범위 밖으로
   명시했습니다.

이번 보완도 GPT 지시대로 범위를 위 4가지로 제한했습니다 — 장애 중
주문 제출·최고가 운영 병합·주문 상태 조회 전용 기능은 이번에도
추가하지 않았습니다.

### 검증

- `test_review_safety_regressions.py`: 31/31 통과 — 기존 "장애
  상태가 장 마감을 막지 않는다" 테스트를 새 기대값(장외에서도
  `reconcile_after_market_close()`를 직접 부르지 않고 `handle_
  balance_outage_tick(reconcile_only=True)`로 위임)에 맞춰
  재작성하고, 신규 3건 추가: 장외 연속 429가 마감 리포트·날짜변경
  확인을 막지 않는지(직접 재현), 다른 API의 429 이후 쿨다운 기간
  동안 `run_once()`가 다시 호출되지 않는지, 장외 복구가 정상 전략
  처리 없이 대조·저장만 수행하는지.
- `test_balance_outage_exit_observation.py`: 45/45 통과(영향 없음
  — `handle_balance_outage_tick()`의 `reconcile_only` 파라미터는
  기본값이 기존 동작과 동일).
- `test_balance_429_fallback.py`, `test_balance_freshness_
  observation.py`: 16/16 통과.
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패는
  이전과 동일한 무관 fixture 누락.
- `legacy_tests/test_entry_watch.py`: 11/11 통과.

### 전달 파일

패치(세분화 커밋 — `trading_service.py` reconcile_only 분기 및
설명 정정, `app/main.py` 장외 분기 재구성 및 다른 API 429 쿨다운,
테스트 파일 갱신, 이 CHANGELOG) + 실제 변경된 파일만 원래 폴더
경로 그대로 담은 diff zip.

---

## 🔧 독립 잔고 재시도 보완 — GPT 8차 재검토 2가지 지적 반영 (2026-09-16, 매매 판단 로직 무변경)

### 배경

GPT가 0028~0031 패치를 검증한 결과, 장외 잔고 백오프·대조 전용
복구·ZIP 정합성은 모두 확인됐으나, 남은 2가지를 지적했습니다.
민우님이 전달한 지시대로 이번 보완 범위는 아래 2건으로 제한하고,
기존에 검증된 복구 응답 전달·장외 주문 미제출·잔고 백오프는
그대로 유지했습니다(장애 중 주문 제출이나 새로운 전략 변경은
추가하지 않음).

### 변경 내용

1. **장외에서 발생한 다른 API 429가 쿨다운을 우회하던 문제
   (높음)**: `reconcile_after_market_close()`는 미해결 주문이
   있으면 잔고 API뿐 아니라 order-status 조회(다른 API,
   `_sync_position_state_machine_shadow()`)도 함께 호출하는데, 그
   429는 `kiwoom_balance_fetch_failure` 태그가 없어 장외 예외
   처리가 로그만 남기고 흡수했습니다 — 다른 API의 429가 매
   폴링(10초)마다 그대로 재시도되는 우회가 있었습니다(GPT 실측
   재현: 대조 재실행 0·10·20초, 백오프 없음). 장중 분기와 동일한
   `other_api_rate_limit_until` 쿨다운을 장외 대조 진입 전
   확인·예외 발생 시 등록하도록 확장했습니다. 쿨다운 변수는
   장중·장외 공통 하나이므로 어느 쪽에서 먼저 걸렸든 다른 쪽으로
   그대로 이어집니다(장중 쿨다운 도중 장외 전환 포함). 날짜변경
   감지·EOD 리포트·취소 처리는 이 쿨다운·대조 성공 여부와 무관하게
   계속 실행됩니다. 잔고 장애와 다른 API 쿨다운이 동시에 걸린
   경우의 우선순위(잔고 장애를 먼저 확인)도 주석으로 명시했습니다
   — `handle_balance_outage_tick()`은 자기 자신의 API만 호출하고
   그 API들도 각자 독립된 백오프·예외 흡수를 갖고 있어, 다른 API
   쿨다운을 우회해 `run_once()`/`reconcile_after_market_close()`
   같은 일반 순회 경로를 다시 타게 만들지 않습니다.
2. **"대조 미완료" 리포트가 대조 완료 후에도 최종 갱신되지 않던
   문제(중간)**: 미해결 주문이 있는 채로 15:20에 마감 리포트를
   잠정 생성한 뒤 `_report_generated_today`가 영구히 `True`가 돼,
   대조가 실제로 끝나도(미해결 주문 해소) 리포트가 다시 생성되지
   않았습니다(GPT 실측 재현: 15:26 잠정 생성 후 15:30 대조 완료에도
   재생성 없음). 또한 "대조 미완료"는 앱 로그에만 남고 리포트
   파일 본문·번들 메타데이터에는 전혀 표시되지 않아, 리포트 파일만
   보는 사람은 잠정 상태임을 알 수 없었습니다. `_report_is_
   provisional` 플래그로 "이번 생성이 잠정본인지"를 별도 추적해
   `_run_end_of_day_tasks()`를 두 개의 독립 트리거로 재구성했습니다
   — 최초 생성(`now.hour == 15 and now.minute >= 20`, 기존과 동일한
   시각 조건)과, 최종 갱신(시각 조건 없이 — 16시 이후 복구
   포함 — 잠정 생성 후 미해결 주문이 해소되면 딱 한 번만 최종본으로
   재생성, 이후 `_report_is_provisional`이 `False`가 돼 중복 생성
   방지). `DailyReporter.generate()`/`_build_report()`에
   `provisional` 인자를 추가해 리포트 파일 본문 최상단에
   "잠정(대조 미완료)" 경고 배너를 남기고, `export_daily_bundle.py`
   가 이미 복사된 리포트 파일에서 그 마커를 확인해 MANIFEST.txt의
   `[ METADATA ]` 섹션에도 동일한 경고를 표시하도록 했습니다.

### 검증

- `test_review_safety_regressions.py`: 34/34 통과 — 신규 3건 추가:
  장외에서 다른 API 429가 발생해도 쿨다운 기간 동안 대조가 재시도
  되지 않고 잔고 장애 상태로도 들어가지 않는지, 장중에 걸린 다른
  API 쿨다운이 장외 전환 후에도 그대로 유지돼 대조를 우회하지
  않는지, 미해결 상태에서 잠정 생성(파일 본문 마커 포함) → 대조
  완료 후 시각 제한 없이(16시 이후 포함) 정확히 한 번 최종본으로
  재생성 → 이후 반복 폴링에서 추가 생성이 없는지(파일 본문 마커
  소거 확인 포함).
- 각 수정 사항은 결함 주입(fault injection) 방식으로 재확인했습니다
  — 장외 쿨다운 로직을 되돌려 신규 2건이 정확히 실패하는지, 최종
  갱신 조건을 임의로 `False`로 고정해 리포트 재생성 테스트가 정확히
  실패하는지, 리포트 본문 배너 삽입을 제거해 같은 테스트가 정확히
  실패하는지 확인한 뒤 모두 원복·재확인했습니다.
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패는
  이전과 동일한 무관 fixture 누락(`test_broker_order_status.py`).
- `legacy_tests/test_entry_watch.py`: 11/11 통과.

### 전달 파일

패치(세분화 커밋 — `app/main.py` 장외 다른 API 쿨다운, `trading_
service.py` 잠정/최종 리포트 트리거, `daily_reporter.py` 파일 본문
배너, `export_daily_bundle.py` MANIFEST 표시, 테스트 파일 갱신, 이
CHANGELOG) + 실제 변경된 파일만 원래 폴더 경로 그대로 담은 diff
zip.

---

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->


## 🔧 독립 잔고 재시도 보완 — GPT 9차 재검토 3가지 지적 반영 (2026-09-16, 매매 판단 로직 무변경)

### 배경

GPT가 0032~0037 패치를 검증한 결과, ZIP ↔ 패치 적용 결과 일치(6개
파일 모두 동일)와 당일 정상 시나리오 개선은 확인됐으나, 완료 처리
전에 세 가지 보완이 필요하다고 지적했습니다. 민우님이 전달한
지시대로 이번 보완 범위는 아래 세 항목으로 제한했습니다 — 장애 중
주문 제출이나 새로운 전략 변경은 추가하지 않았습니다.

### 변경 내용

1. **쿨다운이 겹치면 정상 처리로 우회 진입할 수 있던 문제(높음)**:
   `handle_balance_outage_tick()`은 잔고 재시도가 성공하면 그 안에서
   곧바로 `_run_once_with_balance()`(신규 주문 제출 가능한 정상
   처리)까지 호출합니다. 개정 전(8차까지)에는 장중 분기가 잔고
   장애를 다른 API 쿨다운보다 먼저 확인해서, 두 상태가 동시에
   걸려 있을 때 잔고 재시도가 먼저 성공하면(예: 30초) 다른 API
   쿨다운이 아직 남아있어도(예: 180초) 정상 처리로 우회 진입할 수
   있었습니다(GPT가 두 상태를 함께 주입해 재현). 장외 분기는 이미
   다른 API 쿨다운을 먼저 확인하고 있어 장중·장외의 우선순위가
   서로 달랐습니다. 이제 장중·장외 모두 다른 API 쿨다운을 먼저
   확인하도록 통일했습니다 — 쿨다운 중에는 잔고 재시도 tick 자체를
   이번 폴링에서 건너뛰고, 날짜변경 감지는 이 분기에서도 별도로
   호출해 계속 실행되게 했습니다. 잔고 조회만 먼저 허용하고 "조회
   성공"과 "정상 처리 진입"을 분리하는 건 GPT도 별도 설계가
   필요하다고 밝혀 이번 범위에서는 하지 않았습니다.
2. **최종 보고서 저장 실패가 '최종 완료'로 둔갑하던 문제(중간)**:
   `_generate_daily_report()`가 예외를 삼키고 항상 성공한 것처럼
   반환해, 호출자(`_run_end_of_day_tasks()`)가 저장 성공 여부를 알
   방법이 없었습니다 — 그 결과 최종 갱신 저장이 실패해도 상태가
   "최종 완료"로 확정돼 버렸습니다(GPT 실측 재현: 최종 저장 실패를
   주입해도 생성 시도가 한 번뿐이고 다음 폴링에서 재시도하지
   않음). 이제 저장 성공 여부를 bool로 반환받아, 성공했을 때만
   상태를 갱신합니다 — 실패하면 상태를 그대로 두고(기존 잠정 파일
   보존) 고정 30초 간격(`_eod_report_retry_backoff_seconds`) 후
   재시도합니다.
3. **보고서 상태에 거래일이 없어 익일 복구 시 전일 잠정본을 갱신
   못하던 문제(중간)**: "오늘 이미 생성했는지"/"잠정 상태인지"를
   거래일 정보 없는 bool 두 개(`_report_generated_today`/
   `_report_is_provisional`)로만 추적해, 프로세스가 자정을 넘겨
   계속 실행되는 동안(장외 로직이 전제하는 상황) 전일 잠정본이
   다음날 대조 완료로 최종화될 때 `now.date()`가 이미 다음날로
   바뀌어 있어 리포트·분석·번들이 전일이 아니라 다음날 거래일로
   잘못 생성됐습니다(GPT 실측 재현: 9/16 잠정 생성 → 9/17 대조
   완료 → 번들 대상이 9/17로 바뀜). `_report_generated_date`(당일
   최초 생성 여부)와 `_pending_provisional_date`(최종화를 기다리는
   잠정 리포트의 대상 거래일)를 완전히 분리된 두 값으로 바꿔
   추적하도록 했습니다 — 전일 잠정본이 아직 최종화를 기다리는
   동안에도 당일 리포트를 독립적으로 최초 생성할 수 있고, 전일
   잠정본의 최종화는 그 대상 거래일을 그대로 기억했다가 정확히 그
   날짜로 처리합니다. `_generate_daily_report()`/`DailyReporter.
   generate()` 호출에도 `target_date`를 명시적으로 전달해, 자정을
   넘긴 최종화가 엉뚱한 날짜의 파일을 건드리지 않도록 했습니다.

### 검증

- `test_review_safety_regressions.py`: 37/37 통과 — 신규 3건 추가:
  잔고 장애·다른 API 쿨다운 동시 주입 시 정상 처리로 우회 진입하지
  않는지, 최종 저장 실패 시 상태 유지 후 재시도 성공까지, 자정을
  넘긴 잠정본이 원래 대상 거래일로 정확히 최종화되면서 당일
  리포트도 독립적으로 생성되는지. 기존 잠정/최종 테스트도
  `target_date` 인자 추가에 맞춰 갱신.
- 각 수정 사항은 결함 주입(fault injection) 방식으로 재확인했습니다
  — (1) 장중 분기의 우선순위를 8차 상태로 되돌려 신규 테스트가
  정확히 실패하는지, (2) 저장 성공 여부 반환·상태 갱신 분기를
  제거해 저장-실패 테스트가 정확히 실패하는지, (3) 최종화 대상
  날짜를 다시 `now.date()`로 고정해 자정-교차 테스트가 GPT의 실측
  재현과 동일한 양상(9/17로 잘못 최종화)으로 정확히 실패하는지
  확인한 뒤 모두 원복·재확인했습니다.
- `test_shadow_analysis.py`: 189/189 통과 — `_run_end_of_day_
  tasks()`가 분석·번들 호출에 `now.date()` 대신 `target_date`를
  넘기도록 바뀌어 깨졌던 리터럴 소스 검사(5-1/F-1/F-2) 3건을 새
  호출부에 맞춰 갱신.
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패는
  이전과 동일한 무관 fixture 누락(`test_broker_order_status.py`).
- `legacy_tests/test_entry_watch.py`: 11/11 통과.

### 전달 파일

패치(세분화 커밋 — `app/main.py` 장중 분기 우선순위 정정,
`trading_service.py` 저장 실패 재시도 및 잠정/최종 거래일 분리,
`test_shadow_analysis.py` 리터럴 검사 갱신, `test_review_safety_
regressions.py` 신규 테스트, 이 CHANGELOG) + 실제 변경된 파일만
원래 폴더 경로 그대로 담은 diff zip.

---

## 🔧 마감 리포트 처리 재보완 — GPT 10차 재검토 3가지 지적 반영 (2026-09-17, 매매 판단 로직 무변경)

### 배경

GPT 9차 재검토 보완(패치 0038~0042)을 실측 검증한 결과, 쿨다운
우선순위 통일은 확인됐지만 마감 리포트 처리에서 세 가지 문제가
추가로 발견됐다. GPT의 요청대로 이번 보완은 보고서 처리에만
한정했다 — 쿨다운·잔고 복구·주문 제출·전략 판정 로직은 전혀 건드리지
않았다.

### 지적 1 — 여러 거래일의 잠정 보고서 중 두 번째 날짜가 최종화 대상에서 누락 (중간)

`_pending_provisional_date` 스칼라 하나로만 "최종화 대기 거래일"을
추적해, 연속 이틀이 모두 잠정 상태가 되면 두 번째 거래일이 첫
번째를 덮어썼다(실측: 9/16 미해결 주문 → 9/16 잠정, 9/17에도
미해결 유지 → 9/17 잠정, 이후 미해결 해소 → 9/16만 최종화되고
9/17은 최종화되지 않음). 추가로 "최초 생성" 조건 자체가 `now.hour
== 15`를 요구해, 15:59:50에 최초 저장이 실패하면 16시 이후 영원히
재시도하지 않는 문제도 있었다.

**수정**: 시각 조건은 새 거래일 작업을 등록할 때만 적용하고
(`_report_generated_date`), 등록된 거래일들은 `_eod_pending_dates`
(거래일 → 잠정 대기 여부) dict로 독립 추적한다. 매 폴링마다 오래된
거래일부터(FIFO) 순서대로 처리하며, 재시도 대기 시각도 거래일별
`_eod_retry_at` dict로 분리했다 — 서로 다른 거래일의 재시도가 서로를
밀어내지 않는다.

### 지적 2 — 저장 실패 시 기존 잠정 파일 보존이 실제로는 보장되지 않음 (중간)

9차 구현은 저장 실패 시 "상태를 갱신하지 않아 기존 파일이 보존된다"
고 설명했지만, 실제 파일 쓰기는 `DailyReporter.generate()`가
`Path.write_text()`로 기존 파일에 직접 덮어쓰고 있었다 — 쓰는
도중(디스크 공간 부족 등) 실패하면 파일이 이미 부분적으로 훼손된
뒤였다(실측: 파일을 일부 쓴 뒤 오류를 주입하면 `_generate_daily_
report()`는 정상적으로 False를 반환하지만 기존 파일 내용은 이미
바뀌어 있었음).

**수정**: `DailyReporter._atomic_write_text()`를 추가해, 같은
디렉터리에 임시 파일로 전체 내용을 쓰고 flush·fsync한 뒤 성공했을
때만 `os.replace()`로 원자적으로 교체한다. 쓰는 도중 예외가 나도
기존 파일은 전혀 건드려지지 않고, 실패한 임시 파일은 정리된다.

### 지적 3 — 분석·번들 생성 실패가 재시도되지 않음 (중간)

보고서 저장 직후 실행하는 8개 후속 단계(로그 검증, 시그널/거래/지표
분석, 리플레이, 볼린저 차단 영향, shadow 분석, 번들 내보내기) 중
어느 하나가 실패해도 경고만 남기고 그냥 넘어가 재시도가 전혀
없었다(실측: 번들 실행에 실패 코드를 3번 연속 주입해도 시도는 한
번뿐). 최종 리포트는 갱신됐는데 번들(ZIP)은 예전 잠정본 그대로
남거나 아예 생성되지 않을 수 있었다.

**수정**: 8개 후속 단계 함수 전부가 성공 여부를 bool로 반환하도록
바꾸고, `_eod_followups_pending`(거래일 → 아직 성공 못한 단계 이름
집합)으로 추적해 실패한 단계만 다음 폴링에서 재시도한다. 이미
성공한 단계는 재실행되지 않는다. 리포트가 (잠정이든 최종이든) 다시
저장될 때마다 이 집합은 전부 리셋되어 전체가 다시 시도된다 —
리포트 내용이 바뀌면(특히 잠정→최종 전환) 번들 등도 최신 내용을
반영해 다시 만들어야 하기 때문이다. 검증 자체가 예외로 실패한
경우만 재시도 대상이며, 검증이 정상 실행돼 데이터 품질 문제를
찾아낸 경우(오류/경고 건수 등)는 재시도 대상이 아니다(재실행해도
결과가 바뀌지 않으므로).

### 검증 결과

- `test_review_safety_regressions.py`: 43/43 통과(신규 6건 포함) —
  기존 3건(잠정→최종화, 저장 실패 재시도, 자정 교차)은 새 상태
  구조(`_eod_pending_dates`, `_eod_retry_at`)와 FIFO 우선순위에
  맞춰 갱신.
- 3가지 수정 모두 결함 주입(수정을 되돌려 GPT의 실측 재현과 동일한
  양상으로 정확히 실패하는지 확인한 뒤 원복·재확인)으로 검증:
  (1) 등록 시 dict를 스칼라처럼 덮어쓰게 되돌리면 두 번째 거래일이
  최종화 대상에서 누락되는 것을 재현, (2) `generate()`가 원자적
  저장을 쓰지 않도록 되돌리면 저장 실패 시 기존 리포트가 새 내용으로
  덮어써지는 것을 재현, (3) 후속 단계 결과를 무시하고 항상 전부
  성공 처리하도록 되돌리면 실패한 단계가 재시도되지 않는 것을 재현.
- `test_shadow_analysis.py`: 189/189 통과(변경 없음 — 후속 단계
  실행을 명시적 순차 호출로 구현해 `self._run_shadow_analysis_
  today(target_date)`/`self._export_daily_bundle_today(target_date)`
  리터럴 호출 형태와 순서를 그대로 유지).
- 전체 회귀(`run_regression_tests.py`): 38/39 통과 — 유일한 실패는
  이전과 동일한 무관 fixture 누락(`test_broker_order_status.py`).
- `legacy_tests/test_entry_watch.py`: 11/11 통과.

### 설계 메모 (이번 범위 밖으로 남긴 것)

- 로그 품질 검증(`_validate_logs_today`)의 "성공"은 검사 자체가
  예외 없이 끝까지 실행됐다는 뜻이며, 검사 결과 오류/경고가 없었다는
  뜻이 아니다 — 데이터 품질 문제는 재실행해도 사라지지 않으므로
  재시도 대상으로 삼지 않았다.
- `_atomic_write_text()`는 `tempfile.mkstemp()`를 대상 파일과 같은
  디렉터리에 만들어 `os.replace()`가 같은 파일시스템 내 원자적
  rename이 되도록 했다 — `report_dir` 자체가 네트워크 파일시스템
  등 원자적 rename을 보장하지 않는 환경이면 이 전제가 깨질 수 있다.
- 여러 거래일이 동시에 잠정 대기 상태가 되는 경우(장외 장애가
  이틀 이상 이어짐) FIFO로 순차 처리하므로, 아주 오래 밀린 거래일이
  많이 쌓이면 최종화까지 지연이 누적될 수 있다 — 미해결 주문 판정
  자체가 거래일별로 분리되지 않은 전역 신호 하나이므로, 이는 근본
  원인(장외 장애의 장기화)에서 오는 근본적 제약이다.

### 전달 파일

패치(세분화 커밋 — `trading_service.py`/`daily_reporter.py` 수정,
`test_review_safety_regressions.py` 신규·갱신 테스트, 이 CHANGELOG)
+ 실제 변경된 파일만 원래 폴더 경로 그대로 담은 diff zip.

---

## 🔧 마감 리포트 처리 재보완 2 — GPT 11차 재검토 2가지 지적 반영 (2026-09-17, 매매 판단 로직 무변경)

### 배경

GPT 10차 재검토 보완(패치 0043~0045)을 실측 검증한 결과, 거래일별 dict 추적·16시 이후 재시도·원자적
저장은 확인됐지만, 후속 작업 처리에 두 가지 문제가 추가로 발견됐다. 이번에도 GPT의 요청대로 보완
범위를 "후속 작업의 의존 관계와 재시도 시각 계산"으로 한정했다 — 거래일별 추적·원자적 저장·쿨다운·
잔고 복구·전략 로직은 전혀 건드리지 않았다.

### 지적 1 — 분석 재시도가 성공해도 번들은 예전 상태로 남음 (중간)

**증상 (GPT 실측 재현)**: shadow 분석 실패 → 번들은 예전 내용으로 생성 성공 → shadow 분석 재시도
성공 → 번들은 재생성되지 않음. 실제 호출은 분석 2회·번들 1회, 마지막 대기 목록은 빈 상태였음.

**원인**: `_run_eod_followups()`가 번들(`_export_daily_bundle_today()`)을 다른 7개 분석 단계와
완전히 독립적으로 처리 — 앞선 분석이 아직 실패한(대기 중인) 상태에서도 번들을 그냥 실행해버려,
"예전 분석 결과를 담은 번들"이 성공으로 확정되고 다시는 재생성되지 않았다.

**수정**: `_run_eod_followups()`에서 번들 실행 직전에 `preceding_steps = {"validate", "signal",
"trade", "indicator", "replay", "bb_block", "shadow"}`가 모두 `pending`에서 빠졌는지(=모두 성공)
확인하고, 하나라도 남아 있으면 번들은 이번 폴링엔 건너뛰고 대기 목록(`_eod_followups_pending`)에
그대로 남긴다. 분석 재시도가 성공하는 바로 그 폴링에서(더 기다리지 않고, `_run_eod_followups()`
안에서 나머지 분석을 처리한 직후 이어서) 번들도 최신 결과를 담아 함께 생성된다.

### 지적 2 — 재시도 시각이 작업 시작 기준이라 대기 간격이 보장되지 않음 (중간)

**증상 (GPT 실측 재현)**: 작업 시작 0초 → 번들 실패 확인 40초 → 등록된 재시도 시각이 30초(이미
과거) → 다음 폴링 50초에 바로 재실행 → 실패 후 실제 대기는 10초뿐.

**원인**: `_run_end_of_day_tasks()` 시작 시점에 한 번 구한 `now_mono`를 보고서 저장 실패·후속 단계
실패의 재시도 시각 계산에 그대로 재사용 — 저장이나 subprocess 실행 자체에 걸린 시간(동기식 호출)이
대기 간격에서 그대로 깎여나갔다.

**수정**: 보고서 저장 실패 시(`_eod_retry_at[report_date] = ...`)와 후속 단계 실패 시
(`_run_eod_followups()` 내부) 모두, 실패를 확인한 바로 그 시점에 `self._monotonic()`을 다시 읽어
재시도 시각을 계산하도록 바꿨다. `_run_eod_followups()`의 `now_mono` 매개변수도 제거해 호출자의
(오래된) 시각을 재사용할 여지 자체를 없앴다. 동기식 subprocess 호출의 지연 자체는 이번 범위 밖이며
그대로 남겨뒀다 — 재시도 간격 계산만 바로잡았다.

### 검증 결과

- `test_review_safety_regressions.py`: **45/45 통과**(신규 2건: 번들 선행 의존 + ZIP 내용 검증,
  재시도 시각 계산 검증).
- **결함 주입 2건** 모두 수행(수정을 되돌려 GPT의 실측 재현과 동일한 수치로 정확히 실패 확인 → 원복
  → 재통과 확인):
  1. 번들 선행 의존을 제거하고 예전처럼 독립 실행하도록 되돌림 → 선행 분석 실패 중에도 번들이
     즉시 실행되는 것 재현.
  2. 재시도 시각 계산에 호출 시작 시점의 `now_mono`를 재사용하도록 되돌림 → GPT의 실측 수치
     그대로(작업 시작 0, 실패 확인 40 → 등록된 재시도 시각이 70이 아니라 30) 재현.
- `test_shadow_analysis.py`: **189/189 통과, 변경 없음**.
- 전체 회귀(`run_regression_tests.py`): **38/39 통과** — 유일한 실패는 이전과 동일한 무관 fixture
  누락(`test_broker_order_status.py`, 이번 변경과 무관).
- `legacy_tests/test_entry_watch.py`: **11/11 통과**.

### 전달 파일

- 패치: `0046-fix-report-GPT-11.patch`, `0047-test-report-ZIP-GPT-11.patch`,
  `0048-docs-CHANGELOG-v1.7-2-GPT-11.patch` (0032~0045 적용된 트리 기준으로 이어서 적용)
- `kiwoom_auto_trader_round11_changed_files_20260917.zip` — 변경된 2개 파일
  (`domain/service/trading_service.py`, `test_review_safety_regressions.py`)을 원래 폴더 경로
  그대로 담음.

### 커밋

- `bbc9014` fix(report): 번들 선행 단계 의존 + 재시도 시각을 실패 확인 시점 기준으로 계산 (GPT 11차 재검토)
- `5e93fbe` test(report): 번들 선행 의존 + ZIP 내용 검증 + 재시도 시각 계산 검증 (GPT 11차 재검토)

---

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->

## 🔍 우선순위1 1차 구현 — 체결조회 증거 독립 저장·커버리지 계측 (2026-09-18, 손익 계산 없음/매매 판단 로직 무변경)

### 배경

trades.csv/DailyReporter/RiskManager가 모두 주문 시점 참고가(price)를
"체결가"처럼 쓰고 있어 손익 판단의 기초 자체가 흔들린다는 조사
결과(2026-09-17, `2026-09-17-priority1-fill-ledger-pnl-investigation.md`)
이후, 민우님이 두 차례에 걸쳐 설계를 되돌려 보완을 요구했습니다.
1차 보완(체결 조회 증거를 `TrackedOrderJournalStore`가 아니라 별도
저장소에 영구 보존하고, FILLED 판정과 가격 유효성을 분리하라는
지적)과 2차 보완(반환된 `BrokerOrder`가 첫 매칭 행만 남겨 원본
증거 일부를 잃는 문제, 커버리지를 조회 횟수가 아니라 고유 주문
기준으로 계산해야 하는 문제, 동기 append로는 "매매 루프에 영향
없음"을 보장할 수 없는 문제, 관측일/주문일 혼동, 번들 연결 필요성)를
모두 반영해 v2 설계 문서(`2026-09-18-priority1-stage1-observation-store-
design-v2.md`)를 작성했고, 민우님이 v2 방향을 승인하면서 구현 조건
5가지(브로커 호환성·관측 실패 격리 / ord_qty 포함·실패 조회 기록 /
환경·주문일 구분 커버리지 / 부분 쓰기·재시도·종료 처리 / 계좌 라벨
누락 시 관측만 비활성화)를 지정했습니다. **이번 라운드의 완료
기준은 "확정 손익을 계산했다"가 아니라 "무엇을 조회했고 무엇을
저장했으며 무엇이 빠졌는지 구분할 수 있다"입니다** — 민우님 지시대로
FIFO/손익 계산, RiskManager/DailyReporter 연결, API 조회 빈도 확대는
전혀 하지 않았습니다.

### 변경 내용

1. **`OrderStatusEvidence` — 판정과 원본 증거의 분리 (`domain/models.py`,
   `infra/broker/kiwoom_order_status.py`, `infra/broker/base.py`,
   `infra/broker/kiwoom_broker.py`)**: 기존 `derive_broker_order_status()`
   내부의 `_find_matching()`은 같은 order_id의 첫 매칭 행만 반환해,
   판정에 쓰이지 않은 나머지 응답 행이 그 자리에서 사라졌습니다.
   새 `find_all_matching()`(자매 함수, 기존 함수는 완전히 무변경)이
   같은 order_id의 매칭 행 전체를 순서대로 반환하고, 새
   `build_order_status_evidence()`가 기존 `derive_broker_order_status()`
   결과(무변경)와 전체 매칭 행 목록을 함께 담은 `OrderStatusEvidence`를
   만듭니다. `Broker.get_order_status_evidence()`를 새 추상 아님(기본
   구현 있음) 메서드로 추가했고, 기본 구현은 `get_order_status()`를
   그대로 호출해 매칭 행 없이 감싸기만 합니다 — **오버라이드하지 않는
   모든 브로커(MockBroker, 테스트용 브로커 포함)가 추가 작업 없이
   자동으로 이 메서드를 지원**하며, 기존 `get_order_status()`의 예외
   전파 방식도 그대로 유지됩니다(조건 1). `KiwoomBroker`만 실제로
   오버라이드해 두 응답(`oso`/`cntr`) 전체를 넘깁니다. 증거 구성
   자체가 실패해도(완전히 잘못된 원소 등) `evidence_error`에만 담기고
   `broker_order` 판정은 항상 안전하게 반환됩니다 — 관측 실패가 PSM
   판정을 절대 막지 않습니다.
2. **원문 필드 보강 + 실패 조회 기록 (`infra/storage/order_status_
   observation_store.py`의 `build_entry_evidence()`, `domain/service/
   trading_service.py`)**: 매칭 행을 관측 레코드로 옮길 때
   `ord_qty_raw`(요청 수량 원문, 이전엔 누락)를 포함시켰고, 구조화하지
   않은 나머지 필드는 `raw`에 전체 보존합니다. `_reconcile_tracked_
   order_status()`에서 `query_id`와 시작 시각을 **API 호출 전에** 발급해,
   `get_order_status_evidence()` 호출이 예외로 실패해도 같은 식별자로
   `outcome="api_error"` 관측을 기록합니다(이전엔 성공한 조회만
   기록됐음) — 매매 루프의 예외 처리(로그 남기고 조용히 반환)는
   전혀 바뀌지 않았습니다.
3. **`OrderStatusObservationRecorder` — 제한된 큐 + 별도 기록 스레드
   (신규 파일 `infra/storage/order_status_observation_store.py`)**:
   "매매 루프에 전혀 영향 없음" 대신 "매매 루프가 디스크 완료를
   기다리지 않는다"로 주장 범위를 좁혔습니다. `record()`는 큐가
   가득 차거나(`maxsize=200`) 쓰기가 실패해도 절대 블로킹하지 않고
   `dropped_count`만 올립니다. 쓰기 실패는 같은 파일에 재시도하지
   않습니다(부분 쓰기가 파일 중간에 손상된 행으로 남는 것을 피하기
   위함) — 재시도가 필요하면 호출부가 같은 `write_id`로 새 `record()`를
   넣어야 하며, 그 경우 항상 파일 끝에 새 줄로 추가됩니다. 시작 시
   `_quarantine_incomplete_tail()`이 이전 강제종료로 남은 불완전한
   마지막 줄만 `.corrupt` 사이드카로 격리하고(중간 위치 손상은 격리하지
   않고 CRITICAL만 남김), `shutdown()`은 상한 시간(기본 3초) 안에
   드레인을 시도한 뒤 `clean_shutdown`(제한시간 내 드레인 시도 완료
   여부 — 디스크 확정 의미 아님)/`queue_drained`/`dropped_count`를
   서로 다른 사실로 분리해 종료 마커에 남깁니다. `dedupe_by_write_id()`는
   같은 `write_id`+같은 내용(재시도)은 1건으로 집계하고, 같은
   `write_id`+다른 내용은 조용히 하나를 고르지 않고 충돌로 표시합니다.
4. **환경·주문일 구분 커버리지 (`compute_coverage()`, `infra/storage/
   run_baseline.py` 확장)**: `trades.csv`에는 `account_scope_id`/`env`
   컬럼이 없고 이번 라운드에서도 그 스키마를 바꾸지 않았습니다 —
   대신 기존 `run_baseline.py`(B01, 2026-09-11)에 `account_scope_id`
   필드와 `resolve_env_from_baseline_row()`/`resolve_scope_for_trade_
   timestamp()`(기존 `resolve_run_id_for_timestamp()`를 그대로 재사용하는
   얇은 wrapper)만 추가해 새 조인 로직을 병행하지 않았습니다. 커버리지
   키는 `(account_scope_id, env, order_date, order_id)` 4-튜플이라
   서로 다른 계정/환경/주문일의 같은 order_id를 절대 합치지 않습니다.
   `account_scope_id`가 비어있으면 0%가 아니라 `계측_비활성`으로
   표시합니다(조건 5). 4개 지표(주문 관측률/상태 확인률/가격
   확보율/조회·저장 품질)를 고유 주문 수 기준으로 계산합니다.
5. **계좌 라벨 누락 시 관측만 비활성화 (`config/settings.py`,
   `domain/service/trading_service.py`)**: `BrokerConfig.account_scope_id`
   (기본값 `""`, 하위호환)와 `observation_enabled` 프로퍼티를 추가했고,
   `TradingService.__init__`은 이 값이 비어있으면(공백만 있어도 미설정
   취급) 경고 로그만 남기고 기록기를 `None`으로 둡니다 — 프로그램
   기동 자체는 절대 막지 않습니다. 기록기 생성 자체가 실패해도(기존
   shadow logger들과 동일한 fail-open 패턴) 마찬가지로 `None` 처리됩니다.
6. **일일 번들 연결 (`export_daily_bundle.py`)**: `order_status_
   observations.jsonl`을 날짜로 잘라 `raw/`에 포함하고(말미 불완전
   줄은 그 한 줄만 제외 — 강제종료 추정으로 처리, 조용히 실패하지
   않고 manifest에 남김), `dedupe_by_write_id()`로 재시도/충돌을
   구분한 뒤 `compute_coverage()`로 계정별 커버리지를 계산해
   `metadata/order_status_coverage.txt`로 남깁니다. 기존 `mask()`
   (SENSITIVE_KEYS 기반)를 관측 로그에도 다시 적용해 마스킹이 실제로
   값을 바꿨다면 그 사실 자체를 manifest에 남깁니다. 이 번들은 하루
   단위이므로 **다른 날짜에 접수된 주문에 대한 오늘 조회는 이 번들만
   으로는 order_date를 확정할 수 없어 "미확인"으로 분류됩니다** —
   이는 결함이 아니라 하루 단위 번들의 알려진 한계로 문서화했습니다
   (미래 손익 계산 단계의 확정 키를 지금 정하자는 뜻이 아니라, 현재
   통계에서 서로 다른 주문을 섞지 않기 위함).
7. **테스트 갱신**: `get_order_status(` 호출부를 `get_order_status_
   evidence(`로 개명한 데 맞춰 `test_tracked_order_journal.py`의 호출
   횟수 검사(12-3)를 갱신(기존 검사 의도 — 호출부가 정확히 1곳이라는
   것 — 는 그대로 유지, 이름만 갱신에 맞춤).

### 테스트 및 검증

- 신규 `test_order_status_evidence_observation.py`: **50/50 통과**.
  5가지 구현 조건 + 일일 번들 연결을 각각 그룹으로 검증합니다 —
  (1) 브로커 호환성(오버라이드 없는 브로커도 자동 지원, API 오류
  전파 무변경, 완전히 잘못된 원소 입력에도 크래시하지 않고
  `evidence_error`로 감쌈), (2) `ord_qty_raw` 포함, UNKNOWN 판정에도
  `matched_cntr_entries`에 원문 가격 보존, 실패한 조회도 `query_id`
  발급 후 기록됨(TradingService 통합 테스트), (3) 같은 주문 반복
  조회가 고유 주문 1건으로 집계, 다른 env/주문일의 같은 order_id가
  절대 합쳐지지 않음, 주문일 미확인 시 별도 분리, (4) 종료된
  기록기에도 `record()`가 블로킹하지 않음, 기록 스레드가 멈춰 있어도
  `shutdown()`이 제한시간 안에 반환하며 `clean_shutdown=False`로
  정직하게 표시, 말미 불완전 줄만 `.corrupt`로 격리, write_id
  재시도/충돌 구분, (5) 라벨 미설정 시 `TradingService` 생성이
  크래시 없이 성공하고 `_reconcile_tracked_order_status()`도 정상
  동작, (6) 일일 번들에 관측 raw + 커버리지 요약이 포함되고 말미
  불완전 줄·다른 날짜 관측이 정확히 제외되며 write_id 중복이 1건으로
  집계되는지, 관측 로그 자체가 없는 날도 번들 생성이 실패하지 않는지.
- 기존 `test_order_status_reconciliation.py`(64/64) 무변경 통과 —
  이 파일의 `_ScriptedOrderStatusBroker`는 새 `get_order_status_
  evidence()`를 오버라이드하지 않으므로, 새 호출부를 거쳐도 PSM
  판정·API 호출 횟수가 기존과 완전히 동일함을 실증적으로 확인했습니다
  (조건 1의 핵심 검증 — 별도 fault-injection 없이도 기존 64개
  시나리오가 그대로 통과한다는 사실 자체가 회귀 없음의 증거).
- `test_tracked_order_journal.py`: 67/67 통과(호출부 개명에 맞춘 검사
  갱신 포함).
- `test_export_daily_bundle_run_baseline.py`: 11/11 통과(무변경).
- 전체 회귀(`run_regression_tests.py`): **39/40 통과** — 유일한 실패는
  `test_broker_order_status.py`의 fixture 파일 누락(`tests/fixtures/
  order_reconciliation/20260814_151548_005930_market_buy_full_fill.jsonl`
  이 저장소에 없음). `git stash`로 이번 변경을 모두 되돌린 뒤 같은
  파일이 클린 HEAD에서도 동일하게 실패하는 것을 확인했습니다 —
  **이번 변경과 무관한 기존 결함**입니다.
- `legacy_tests/test_entry_watch.py`: 11/11 통과.

### 변경하지 않은 것

- FIFO 매칭, 확정 손익 계산, RiskManager/DailyReporter와의 연결 —
  이번 라운드는 "무엇을 조회·저장했는지 구분할 수 있는" 단계까지만
  입니다(민우님 지시).
- 기존 API 조회 정책(폴링당 최대 1건, 30초 이상 대기/orphan만 대상),
  `_select_order_status_query_target()`의 우선순위 로직, PSM의
  BUY_PENDING/SELL_PENDING/ORPHAN 상태 전이 판정, 주문 접수·리스크
  게이트 — 전혀 건드리지 않았습니다.
- `TrackedOrderJournalStore`는 여전히 안전한 시점에 레코드가
  삭제되는 기존 동작 그대로이며, 새 관측 저장소는 이 저널을 전혀
  재사용하지 않습니다(2차 지적 반영 — 삭제되는 저장소를 영구 증거로
  쓰지 않음).
- B(확정 체결 키)의 설계는 이번 라운드에서 확정하지 않았습니다 —
  민우님 지시대로 보류 상태 그대로입니다.
- `app/main.py`의 정상 종료(graceful shutdown) 경로에서
  `OrderStatusObservationRecorder.shutdown()`을 호출하는 배선은
  **의도적으로 이번 라운드에 포함하지 않았습니다.** `async_main()`이
  약 600줄, 두 갈래(WebSocket 조건검색 감시 모드 / 단순 폴링 모드)의
  복잡한 제어 흐름을 갖고 있어, 그 구조를 충분히 검증하지 않은 채
  종료 경로에 손을 대면 오히려 기존 종료 처리(다른 로거/저장소의
  플러시 등)를 깨뜨릴 위험이 이번 관측 기능이 주는 이득보다 크다고
  판단했습니다. 이번 라운드가 배선 없이도 안전한 이유: 기록기는
  daemon 스레드이므로 프로세스가 그냥 종료돼도 매달리지 않고, 매
  `_write_one()` 호출마다 `fsync`를 시도하므로(TrackedOrderJournalStore와
  동일 관례) 정상 종료 배선이 없어도 대부분의 기록은 이미 디스크에
  반영된 상태입니다 — 다만 `shutdown()`이 명시적으로 호출되지 않으면
  종료 마커(`clean_shutdown` 등) 자체가 남지 않고, 딱 그 시점에 큐에
  남아있던(아직 쓰기 전) 레코드는 유실될 수 있습니다. 다음 작업으로
  제안합니다.
- 쓰기 실패에 대한 제자리 재시도(retry-in-place)는 구현하지 않았습니다
  (민우님 3차 지적 반영) — 실패한 레코드는 버리고 `dropped_count`만
  올립니다. 호출부 수준의 재시도(같은 `write_id`로 새 `record()` 호출)는
  항상 파일 끝에 새 줄로 붙으므로, export 단계의 "말미 한 줄만
  불완전할 수 있다"는 가정이 깨지지 않습니다 — 다만 이번 라운드에는
  그 호출부 수준 재시도 자체도 아직 추가하지 않았습니다(단순 유실
  계측까지만).

### 다음 작업

1. `app/main.py`의 정상 종료 경로에 `OrderStatusObservationRecorder.
   shutdown()` 호출을 추가하는 작은 후속 패치(위 "변경하지 않은 것"
   참고 — 이번 라운드에서 충분히 검증하지 못해 분리함).
2. `test_broker_order_status.py`의 fixture 디렉터리
   (`tests/fixtures/order_reconciliation/`)가 저장소에 커밋돼 있는지
   확인 — 없다면 `.gitignore` 설정 오류이거나 원래 별도 경로에서
   받아와야 하는 자산일 수 있습니다(이번 라운드와 무관하지만 방치되고
   있는 회귀 실패이므로 확인 필요).
3. (1차 구현 완료 후, 민우님 확인 시) 이번에 쌓인 관측 데이터의
   커버리지가 실제 운영에서 충분한 수준(예: 주문 관측률)에 도달하는지
   1~2거래일 지켜본 뒤, B(확정 체결 키)·FIFO 손익 연결 설계를 별도로
   요청할 수 있습니다 — 이번 라운드 범위 밖.

### 전달 파일

- 패치(세분화 커밋, 0032~0050 적용된 트리 기준으로 이어서 적용 —
  0049/0050은 GPT 11차 재검토 라운드의 CHANGELOG 문구 정정 2건이며
  이미 반영된 상태를 기준으로 함):
  - `0051-feat-broker-order-status-evidence.patch` —
    `domain/models.py`, `infra/broker/kiwoom_order_status.py`,
    `infra/broker/base.py`, `infra/broker/kiwoom_broker.py`
  - `0052-feat-config-account-scope-id.patch` — `config/settings.py`
  - `0053-feat-run-baseline-account-scope-id.patch` —
    `infra/storage/run_baseline.py`
  - `0054-feat-order-status-observation-store.patch` — 신규 파일
    `infra/storage/order_status_observation_store.py`
  - `0055-feat-trading-service-observation-wiring.patch` —
    `domain/service/trading_service.py`
  - `0056-test-tracked-order-journal-rename.patch` —
    `test_tracked_order_journal.py`
  - `0057-test-order-status-evidence-observation.patch` — 신규 파일
    `test_order_status_evidence_observation.py`
  - `0058-feat-export-daily-bundle-observation.patch` —
    `export_daily_bundle.py`
  - `0059-docs-CHANGELOG-v1.7-priority1-stage1.patch` — 이 CHANGELOG
- `kiwoom_auto_trader_priority1_stage1_changed_files_20260918.zip` —
  실제 변경된 12개 파일(위 목록 그대로)을 원래 폴더 경로 유지한 채
  담음.
