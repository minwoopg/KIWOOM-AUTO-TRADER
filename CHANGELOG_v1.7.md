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
