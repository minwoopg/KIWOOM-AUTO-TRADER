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

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->
