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
| 🔴 최우선 | Candidate A pilot B단계 | 8/28 실제 종목(122630/069500/102110) 분봉이 차단 시점+20분까지 확보되는지 실측 → 오프라인 반사실 재구성 → 통과 시 C단계(`shadow`→`enforce`) 별도 승인 |
| 🟡 중간 | MIN_PROFIT_5M 구조 분석 | 18건 중 15건 손실(-551,068원)의 진입시점 feature를 정상 청산 8건과 univariate 비교 — 아직 착수 전 |
| 🟢 낮음 | 원 6단계 계획 잔여분 재검토 | `decision_engine_mode`/`position_lifecycle_mode`/`reward_risk_guard_mode`/`candidate_ranking_mode`/`trailing_breakeven_mode` 5개 flag가 계속 off로 남아 있는 게 여전히 맞는 판단인지 정리 |

---

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->
