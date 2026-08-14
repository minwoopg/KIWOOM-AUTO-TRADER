# 주식 자동매매 프로그램 ver1.6 패치노트

> 작성일: 2026-07-27
> 대상 기간: 2026-07-24(v1.5) ~ 진행중

---

## 작업 개요

v1.5까지는 RiskManager 손익계산·포지션 상태머신(shadow)의 안전성을
GPT 코드리뷰와 13라운드에 걸쳐 정밀 검증했습니다. v1.5 완료 시점에
GPT가 실제 소스 전체(51개 Python 파일)를 대상으로 **"왜 수익률이
안 나오는가"**를 구조적으로 재검토했고, 그 결과 다음 핵심 문제들이
확인됐습니다.

1. `day_high`/`day_low`/`vwap`이라는 이름과 달리 실제로는 "최근
   60분" 값(당일 전체 아님) — `minute_bar_count: 60` 고정, 키움
   API도 `raw_bars[:count]`로 정확히 60개만 자름 (코드로 재현 확인)
2. 진입 점수 8점 중 상당수가 1시간에 한 번만 갱신되는 일봉 지표
   (RSI/MACD)와 섞여 있어, 장중 실시간 타점 신호와 괴리
3. 상승여력 게이트가 점수 5점 이상이면 우회 가능한 소프트 필터
   (`min_score = 5 if low_upside_gate_active else 3`)
4. 종목 후보를 서로 비교하지 않고 순회 중 먼저 조건을 통과한
   종목을 즉시 매수 — "최선"이 아니라 "먼저 온 것"을 삼
5. `place_order()`의 `accepted=True`를 실제 체결로 취급 (포지션
   상태머신은 이미 있지만 여전히 shadow 모드)
6. 리플레이가 실제 매매 로직(StrategyRouter/RiskManager/entry_watch
   등)을 거치지 않아 백테스트로 신뢰하기 어려움
7. 장세가 바뀌면 보유 포지션의 청산 정책 자체가 교체될 수 있음
8. 트레일링 스탑이 수익 구간에서도 순손실로 끝날 수 있는 구조

GPT가 제안한 "전체 갈아엎기" 대신, 이번 세션에서 검증된 리스크
관리·로깅·상태머신 인프라는 유지하면서 **의사결정 데이터 정확성과
의사결정 구조**만 단계적으로 교체하는 방향으로 합의했습니다(Claude
초안 제안에 GPT가 순서 조정 의견을 더해 최종 확정).

### 최종 합의된 단계

| 단계 | 내용 | 실거래 영향 |
|---|---|---|
| 0 | 테스트·기준선·기능 플래그 | 없음 |
| 1 | 세션/롤링 데이터 의미 분리 | shadow |
| 2 | DecisionEngine 추출 (조건 변경 없음) | 없음 |
| 3 | 체결 확인·상태머신·원장 실제화 | 안전성 개선 |
| 4 | 손익비 하드 게이트 | shadow → enforce |
| 5 | 후보 순위화 | shadow → enforce |
| 6 | 순본전 트레일링 | shadow → enforce |

GPT 조정안의 핵심 — Claude 초안 대비 **체결 원장 실제화(3단계)를
앞당기고**, 세션 지표는 기존 롤링 지표를 대체하지 않고 **병존**시키며,
**0단계에 테스트/전체 스냅샷 복구를 명시적으로 추가**함.

---

## 0단계: 기준선 고정과 안전장치 (2026-07-27, 진행중)

### 전체 소스 스냅샷 제공 (GPT 지적 대응)

GPT가 "전달받은 ZIP에 CHANGELOG가 언급하는 test_*.py 파일들이
없다"고 지적 — 실제로는 이번 세션 작업 환경(Claude 컨테이너)에는
24개 테스트 파일이 전부 남아있었지만, 최근 여러 라운드에 걸쳐
diff(변경분)만 전달해서 민우님과 GPT 양쪽 다 "완전한 최신 전체
스냅샷"을 한 번도 통째로 받지 못한 상태였음 — 이번에 처음으로
`kiwoom-auto-trader-v1.5-full-snapshot.zip`(logs/data/reports
제외, 소스+테스트 114개 파일, 350KB)로 통합 제공. 스냅샷 위치에서
재컴파일 및 25개 테스트 파일 전체 재실행으로 정상 동작 확인.

### 기능 플래그 골격 추가

`config/settings.py`에 `ExperimentalConfig` 신규 — 6개 리팩터링
단계 각각을 `"off"`/`"shadow"`/`"enforce"` 세 상태로 제어하는 플래그.
전부 `"off"`로 시작해 이 커밋 자체는 어떤 동작도 바꾸지 않음(이후
단계가 이 플래그를 실제로 참조하기 시작할 때부터 의미가 생김).

```yaml
experimental:
  session_metrics_mode: "off"       # 1단계
  decision_engine_mode: "off"       # 2단계
  position_lifecycle_mode: "off"    # 3단계
  reward_risk_guard_mode: "off"     # 4단계
  candidate_ranking_mode: "off"     # 5단계
  trailing_breakeven_mode: "off"    # 6단계
```

**구현 중 발견한 함정**: YAML 1.1 스펙에서 따옴표 없는 `off`/`on`/
`yes`/`no` 등은 boolean으로 자동 해석됨 — 실제로 `session_metrics_
mode: off`(따옴표 없이)로 작성했더니 파싱 시 문자열이 아니라
`False`로 들어와 검증에서 걸리는 것을 재현. `settings.yaml`에는
`"off"`처럼 반드시 따옴표로 감싸 작성, `ExperimentalConfig.
__post_init__()`에도 `isinstance(value, str)` 검사를 추가해 이
함정이 조용히 통과되지 않고 명확한 오류로 잡히도록 함(오류 메시지
안에 원인과 해결법을 함께 명시).

**검증**: `test_experimental_config.py` — 7건 전부 통과. 실제
`settings.yaml`에서 정상 파싱되는지, 전부 `"off"`로 시작하는지,
YAML boolean 오염(`False`/`True`)이 명확한 `ValueError`로 거부되는지,
잘못된 문자열 값도 거부되는지, 정상적인 off/shadow/enforce 조합은
통과하는지, `experimental` 섹션 자체가 없는 YAML도 기본값(전부
off)으로 안전하게 로드되는지까지 확인. 전체 회귀 기존 24개 파일
재통과 확인 — 총 25개 테스트 파일.

**남은 0단계 작업**: 기존 회귀 테스트가 이미 상당수 존재하지만
(entry_watch, 재진입 제한, 연속손실 차단, FIFO 손익, 부분체결 등),
GPT가 요청한 "1~6단계 착수 전 최종 기준선" 성격의 통합 스냅샷
테스트(오늘자 실제 신호 흐름 스냅샷 비교)는 아직 미착수 — 1단계
착수 시 함께 준비.

---

## 0.5단계: 테스트 인프라 정리 및 안전장치 보강 (2026-07-27)

**배경**: 0단계 스냅샷을 GPT가 실제로 실행해보고 4가지 문제를
발견 — 방향은 승인하되 1단계 착수 전에 먼저 정리가 필요하다는
지적.

### 1. tests/ 디렉토리 중복·구버전 정리

`tests/`에 프로젝트 루트와 동일한 파일명 4개(`test_entry_watch.py`,
`test_position_priority_order.py`, `test_run_once_integration.py`,
`test_sold_today_and_reentry_fix.py`)가 있어 `pytest -q` 실행 시
`import file mismatch`로 수집이 실패하는 것을 확인. 그중
`tests/test_sold_today_and_reentry_fix.py`를 실제로 실행해보니
6건 중 2건 실패 — 지난 라운드(v9~v10 근처)의 정책 변경(trades.csv
파일 미존재 시 fail-open→fail-close 전환)에 맞춰 루트 버전은
갱신했지만 `tests/` 안의 사본은 놓쳤던 것으로 확인. `tests/test_
breakout_strategy.py`(중복은 아니지만 pytest 클래스 스타일의
독립 파일)도 실행해보니 `StrategyConfig`에 트레일링 관련 필드가
추가된 이후 시그니처가 안 맞아 2건 모두 `TypeError`로 실패.

`tests/` 디렉토리의 5개 파일을 전부 `legacy_tests/`로 이동하고
`tests/`는 제거. `legacy_tests/README.md`에 왜 옮겨졌는지, 검증
내용이 어느 최신 테스트로 대체됐는지 기록 — 완전 삭제 대신
이동으로 이력 보존.

### 2. Settings.experimental을 default_factory로 전환

기존 `experimental: ExperimentalConfig = None`은 `load_settings()`
를 거치면 항상 채워지지만, `Settings(...)`를 직접 생성하는 코드
(테스트 등)에서는 `None`으로 남아 `settings.experimental.xxx_mode`
접근 시 `AttributeError`가 남을 재현 확인. `field(default_factory=
ExperimentalConfig)`로 전환 — 어떤 방식으로 `Settings`를 생성해도
항상 유효한 `ExperimentalConfig`(전부 off)가 채워지도록 모델
자체가 보장하게 함(호출부마다 `settings.experimental or
ExperimentalConfig()`를 기억해야 하는 구조보다 안전).

### 3. 테스트 산출물이 실제 logs/에 새는 문제 수정

스냅샷 zip에 `logs/entry_watch_shadow.csv`, `logs/position_
lifecycle.csv`가 섞여 들어간 것을 발견 — 원인 추적 결과,
`test_run_once_integration.py`의 `build_minimal_settings()`가
`StorageConfig` 생성 시 `entry_watch_shadow_log_file`/`position_
lifecycle_log_file` 두 필드를 명시하지 않아, `StorageConfig`의
기본값(`"logs/entry_watch_shadow.csv"`, `"logs/position_lifecycle.
csv"` — 상대경로)이 그대로 쓰이고 있었음. 이 설정을 재사용하는
다른 테스트들을 실행할 때마다 프로젝트 루트의 실제 `logs/`
디렉토리에 CSV가 계속 쌓이고 있었던 것(재현 확인). 두 필드를
`tmpdir` 기준 경로로 명시 — 이후 `logs/position_lifecycle.csv`의
mtime이 관련 테스트 6개를 실행해도 변하지 않는 것으로 수정 확인.

### 4. 공식 회귀 테스트 실행 스크립트 추가

`run_regression_tests.py` 신규 — 프로젝트 루트의 `test_*.py`를
`subprocess`로 순차 실행하고 결과를 요약, 하나라도 실패하면
`exit code 1`을 반환. 이 프로젝트는 pytest 프레임워크를 쓰지
않고 각 테스트가 독립적인 assert-print-exit 스크립트로 작성되어
있어(파일 끝의 `sys.exit(1) if failed`), pytest로 수집하는 것보다
각 파일을 그대로 실행하는 방식이 이름 충돌이나 시그니처 불일치
문제를 피할 수 있어 더 안전하다고 판단. `--pattern`으로 일부만
선택 실행, `-v`로 상세 출력도 지원. `python run_regression_tests.py`
실행 결과 25개 파일 전부 통과, 총 소요시간 약 6초. 의도적으로
실패하는 테스트를 만들어 `exit code 1`이 정확히 반환되는지도
별도 검증.

`README.md`에 "테스트" 섹션 신규 추가 — 공식 실행 명령과
`legacy_tests/`의 성격을 명시.

**전체 회귀**: `run_regression_tests.py`로 25개 파일 전부 통과 확인
(위 수정 3건이 기존 테스트 결과에 영향 없음을 함께 확인).

**남은 GPT 지적 사항 (아직 미착수)**: 7월 24일 실제 데이터 기반
legacy 기준선 fixture·expected_decisions.json — 이건 1단계
(`raw_bars` 진단 로깅, 세션 데이터 부트스트랩 설계)와 함께
착수 예정. `git checkout -b refactor/v1.6-stage0` 브랜치 분리와
`v1.6-stage0-baseline` 태그는 다음 커밋 시 로컬에서 적용 안내
예정(이 세션에서는 원격 git 조작 권한이 없어 커밋 메시지로 안내).

---

## 0.5단계 마무리: .gitignore (2026-07-27)

GPT 코드리뷰에서 0.5단계 자체는 승인하되, `.gitignore`가 없어
실제 계좌 로그·`.env`·캐시가 다시 커밋될 위험이 있다는 지적을
받음.

기존 `.gitignore`(`logs/`, `data/` 전체를 통째로 무시, `*.csv`
전역 무시)를 재작성 — `logs/*` + `!logs/.gitkeep` 형태로 예외
처리를 명시하고, 앞으로 `tests/fixtures/`에 둘 분봉/일봉 CSV
픽스처가 전역 `*.csv` 규칙에 실수로 걸리지 않도록
`!tests/fixtures/**`를 추가. `.pytest_cache/`, `.mypy_cache/`,
`reports/*`도 신규 반영.

임시로 `git init` 후 `git check-ignore -v`로 실제 규칙 매칭을
검증 — `tests/fixtures/legacy_20260721/*.csv`는 무시되지 않고
(커밋 대상 유지), `logs/trades.csv`는 정확히 무시되는 것을 확인
후 `.git` 제거(이 세션은 실제 저장소가 아닌 검증용).

**전체 회귀**: `run_regression_tests.py` — 25개 파일 전부 통과,
종료코드 0. 테스트 실행 전후 `logs/`/`data`/`reports`에 새로 생긴
파일 없음(재확인).

---

## 1A단계: Legacy 기준선 고정 (2026-07-27)

**중요한 사실 확인**: 작업 지시에는 2026-07-24 데이터를 기준으로
하라고 되어 있었으나, 작업 환경(`data/minute_bars/`, `logs/
signal_log.csv`, `logs/trades.csv`)을 전부 확인한 결과 **7월 24일
데이터가 어디에도 존재하지 않음**을 확인(`data/minute_bars/`의
가장 최근 날짜는 7월 21일). 지시받은 원칙(실제 데이터가 없으면
임의 생성하지 않고 보고)에 따라, 존재하는 가장 최근 거래일인
**2026-07-21** 데이터로 대체해서 진행. 로컬에 7/24 데이터가 남아
있다면 다시 만들 수 있도록 fixture README에 안내 남김.

**fixture 구성**: `tests/fixtures/legacy_20260721/` 신규.
`logs/signal_log.csv`(7/21, 4499건)에서 다양성을 확보한 10개
판단 시점을 선정 — 실제 BUY 1건, BLOCKED 2건(재진입쿨다운/일일
한도), HOLD(SKIP) 7건(점수부족/VWAP아래/상승여력부족/눌림범위밖/
거래량없음/지표없음×2), 5개 서로 다른 종목(475150/005930/114800/
252670/012690) 포함. 해당 종목들의 `data/minute_bars/20260721/
*.csv`를 그대로 복사, `data/state.json`(민감정보 없음 확인 —
계좌번호/토큰 없이 내부 상태값만 존재)을 `runtime_state.json`
으로, `config/settings.yaml`(이미 `${ENV_VAR}` 참조 형태라 실제
값 없음)을 그대로 포함.

**fixture 제작 중 실제로 발견한 사실 (1B/1C 단계에 중요한 단서)**:
1. **전일 봉 혼입이 이미 저장 파일 수준에서 존재**: `475150.csv`
   (320행) 중 46건이 7/20 봉, `005930.csv`(319행) 중 45건이 7/20
   봉. `012690.csv`(61행)는 오히려 대부분(58건)이 7/20이고 7/21은
   단 2건뿐 — 이 종목의 실제 판정이 `SKIP_NO_VOLUME`이었는데,
   당일 데이터 자체가 거의 없어서 나온 결과일 가능성을 시사(1B/1C
   단계에서 검증 필요, 현재는 추측 단계).
2. **저장된 CSV는 이미 가공된 데이터**: `KiwoomBroker.get_minute_
   bars()`가 `bars.reverse()`로 정렬한 뒤 저장된 결과라, API
   원본의 실제 반환 순서·개수는 이 fixture로 알 수 없음 — 1B
   단계에서 실제 API 호출 시점 raw 응답을 별도로 로깅해야 함.
3. **일봉 캐시 부재**: `data/`에 일봉 캐시가 없어, RSI/MACD 등
   일봉 기반 지표의 원본 입력은 fixture에 포함 못 함. 대신
   `signal_log.csv`에 이미 계산되어 기록된 지표값(`atr_14`,
   `bb_percent_b`, `ma5_above_ma20` 등)을 그대로 보존 — "재계산
   검증"은 못 하고 "당시 기록값의 동결"만 가능.

**아직 하지 않은 것**: 이 fixture의 분봉 데이터를 실제
`MinuteAnalyzer`/`StrategyRouter`에 넣어 `expected_decisions.json`
과 정확히 일치하는지 비교하는 재현 테스트는 아직 작성하지 않음
— `signal_log.csv`의 값은 실거래 중 전체 컨텍스트(캐시 상태,
폴링 타이밍, 일봉 지표 등)가 반영된 결과라 분봉 재생만으로는
정확히 같은 파이프라인을 재현하기 어려워, 이 재현 자체가 다음
작업 대상. 현재 작성한 `test_legacy_baseline_fixture.py`는
"fixture 자체가 손상되지 않고 그대로 유지되는지"만 검증하는
무결성 테스트(18건).

**검증**: `test_legacy_baseline_fixture.py` — 18건 전부 통과.
fixture 필수 파일 존재, `expected_decisions.json` 10건과 필수
필드, 케이스 다양성(BUY/BLOCKED 2건 이상/HOLD 다수/5종목 이상),
각 케이스 종목에 대응하는 분봉 CSV 존재, 분봉 CSV 파싱 가능성과
필수 컬럼, `runtime_state.json`에 민감정보 의심 키 없음,
`settings.yaml`이 환경변수 참조 형태(실제 값 아님)인지까지 확인.
전체 회귀 기존 25개 파일 재통과 확인 — 총 26개 테스트 파일.

**다음 단계 전제조건**: GPT 지시대로 "이 단계 결과가 정상일 때만
1B/1C로 진행" — 위 결과를 먼저 보고하고 확인받은 뒤 1B(raw_bars
진단 로깅)로 진행. `v1.6-stage0-baseline` 태그는 이 fixture가
포함된 커밋 이후에만 붙이는 것으로 GPT와 합의된 순서를 따름(0.5
단계 커밋에는 태그 없음).

---

## 1A 보완: fixture를 "재현 검증 가능" 수준으로 강화 (2026-07-27)

**배경**: 1A 1차 결과에 대한 GPT 코드리뷰에서, 실제로는
"의사결정 기준선 재현"이 아니라 "과거 자료 아카이브 및 구조
검사" 수준이라는 정확한 지적을 받음(승인은 하되 5가지 보완 요청).

### 1. 테스트 명칭·목적 수정

`test_legacy_baseline_fixture.py` → `test_legacy_fixture_structure.py`
로 파일명 변경. "baseline"이라는 이름이 "의사결정 재현까지
검증한다"는 오해를 줄 수 있다는 지적을 받아들여, 실제 검증 범위
(구조·해시 무결성)를 정확히 반영한 이름으로 교체. `README.md`에
1A-1~1A-5 단계별 완료 상태표를 추가해 "구조 검증 완료"와 "재현
미완료"를 명확히 구분.

### 2. SHA-256 manifest 추가

`tests/fixtures/legacy_20260721/manifest.sha256` 신규 —
`expected_decisions.json`/`settings.yaml`/`end_of_capture_
runtime_state.json`/`minute_bars/*.csv` 전체의 SHA-256 기록.
`test_legacy_fixture_structure.py`가 이 manifest를 실제로 재계산해
일치 여부를 검증 — manifest이 모든 재현 입력 파일을 빠짐없이
커버하는지, manifest 자신은 목록에서 제외됐는지까지 확인.

**검증**: `expected_decisions.json`의 `final_decision`을 `"BUY"`
→`"SELL"`로 조용히 변조한 뒤 테스트를 실행해 실제로 `FAIL`이
나는 것을 확인(GPT가 지적한 정확한 시나리오 — 기존 구조 검증만
으로는 이런 변경을 못 잡았음) — 이후 원상복구해서 재검증.

### 3. 미래 데이터 누출 방지 메타데이터

GPT 지적을 실제 데이터로 재현 확인 — `실제_BUY`(09:16:45) 케이스는
`475150.csv` 전체 319행 중 63행만 판단 시점 이전/동일이고 나머지
256행이 미래 봉. 10개 케이스 전부에 대해 정확한
`bar_cutoff_timestamp`와 `expected_bars_at_or_before_cutoff`를
직접 계산해 `expected_decisions.json`에 추가(63/75/125/106/61/
60/60/60/61/61). `test_legacy_fixture_structure.py`가 이 값들이
실제 분봉 CSV와 정확히 일치하는지, 그리고 최소 하나의 케이스에서
cutoff 이후 미래 봉이 실제로 존재한다는 사실 자체도 검증.

### 4. runtime_state.json 성격 수정

GPT 지적을 실제 데이터로 재현 확인 — `475150`의 `last_sold_at`/
`peak_price`/`symbol_stoploss_at`이 전부 13:30:14(오후) 시각인데,
fixture의 첫 BUY 판단은 09:16:45. 즉 "판단 시점 상태"가 아니라
"캡처 종료 시점 상태"였음. 파일명을 `end_of_capture_runtime_
state.json`으로 변경, 불필요한 운영 메타데이터인
`last_order_id_by_symbol`(56건) 제거한 축소본으로 교체. 각
`expected_decisions.json` 케이스에 `"risk_context_available":
false`를 명시 — 케이스별 정확한 시점의 `RuntimeState`를 복원할
자료가 없다는 사실을 임의로 채우지 않고 있는 그대로 표시.

### 5. Git 명령 수정

GPT 지적대로 첫 `git add`가 모든 변경을 가져가면 두 번째 커밋이
비는 문제 — 이번 보고에서는 커밋별로 정확히 분리된 `git add`
명령을 제공(아래 git 명령 섹션 참고).

**전체 회귀**: `run_regression_tests.py` — 26개 파일 전부 통과,
종료코드 0. `test_legacy_fixture_structure.py` 단독 28건 전부
통과(구조 검증 18건 + manifest/cutoff 관련 신규 10건).

**여전히 미해결(GPT도 인정한 제약)**: 일봉 지표 원본 부재,
케이스별 RiskManager 상태 부재 — 이건 자료 자체가 없어서 이번
보완으로 해결 불가능한 근본적 제약이고, README에 명확히 기록됨.

---

## 1A.5: legacy 60봉 입력창 메타데이터 (2026-07-27)

**배경**: 1A 보완 결과에 대한 GPT 2차 코드리뷰에서, 미래 데이터
누출 방지 메타데이터(`bar_cutoff_timestamp`)만으로는 부족하다는
지적을 받음 — "cutoff 이전 전체 봉"과 "실거래 코드가 실제로 쓴
입력(cutoff 이전 중 최신 60개)"이 다르다는 것.

**검증**: `config/settings.yaml`의 `minute_bar_count: 60`을
재확인하고, `실제_BUY`(09:16:45) 케이스로 직접 계산 — cutoff
이전 전체 63개 중 최신 60개만 실거래 입력이었고, 그 범위가
`20260720143900`(전날 14:39) ~ `20260721091600`(당일 09:16)임을
확인. GPT가 표로 제시한 수치와 정확히 일치. 10개 케이스 전부
재계산한 결과, **5개 케이스(개장 직후)가 실제로 전일 봉을
포함**하는 것을 확인 — 5개(시간이 충분히 지난 케이스)는 전일
봉 없이 60개를 채움. (2026-07-27 정정: 최초 보고에서 "6개"로
잘못 기록했던 것을 GPT 2차 코드리뷰로 발견 — 직접 재집계한 결과
정확히 5개(`실제_BUY`, `BLOCKED_재진입쿨다운`, `SKIP_상승여력
부족`, `SKIP_눌림범위밖`, `SKIP_거래량없음`)로 확인, 아래 1A.6절
참고.)

**반영**: `expected_decisions.json`의 10개 케이스 전부에
`legacy_requested_bar_count`(60), `expected_legacy_input_count`,
`expected_legacy_first_timestamp`, `expected_legacy_last_
timestamp`, `legacy_input_contains_prior_date` 5개 필드 추가.
`manifest.sha256` 재생성(`expected_decisions.json` 해시만 변경,
분봉/설정/runtime_state는 무변경). `test_legacy_fixture_
structure.py`에 7-1번 검증 섹션 추가 — `eligible[-60:]` 계산
규칙으로 각 케이스의 개수/첫/끝 timestamp/전일봉포함 여부가
실제 분봉 CSV와 정확히 일치하는지, 최소 한 케이스는 실제로
전일 봉을 포함하는지 확인. `REQUIRED_FIELDS`에도 5개 필드 추가.

**검증**: `expected_legacy_input_count`를 60→59로 조용히 변조한
뒤 테스트 실행 — manifest 해시 불일치와 7-1번 검증 둘 다 FAIL로
잡아내는 것을 확인(이중 방어). 원상복구 후 재검증.

**의미**: 이건 단순한 "최근 60분" 문제가 아닙니다. 저장된 누적
분봉을 cutoff로 잘라 사후 복원한 결과이며, **장 초반 입력창에
전날 오후 봉과 당일 장 초반 봉이 섞여 있었을 가능성을 강하게
뒷받침합니다.** 당시 캐시의 정확한 입력은 1B 진단 도입 이후부터
확인할 수 있습니다(1A.6절에서 이 표현을 다시 명확히 함 — 최초
작성 시 "확정"이라고 썼던 것은 과한 표현이었음). 어느 쪽이든
1단계(세션/롤링 데이터 분리) 착수 사유를 구체적으로 뒷받침합니다.

**전체 회귀**: `run_regression_tests.py` — 26개 파일 전부 통과,
종료코드 0. `test_legacy_fixture_structure.py` 단독 30건 전부
통과(28건 + legacy 60봉 검증 2건).

---

## 1A.6: 문서 오류 정정 + fixture 검증 전제 보강 (2026-07-27)

**배경**: 1A.5 결과에 대한 GPT 2차 코드리뷰에서 문서 오류 1건과
검증 누락 4건을 지적받음.

### 1. 전일 봉 포함 케이스 개수 오류 정정 (6개 → 5개)

`expected_decisions.json`을 직접 재집계한 결과
`legacy_input_contains_prior_date=true`인 케이스는 정확히 **5개**
(`실제_BUY`, `BLOCKED_재진입쿨다운`, `SKIP_상승여력부족`,
`SKIP_눌림범위밖`, `SKIP_거래량없음`)임을 확인 — 이전 보고에서
"6개"라고 잘못 기록했었고, fixture README에는 "6개"라고 써놓고
실제로는 5개만 나열하는 내부 모순까지 있었음(GPT 지적, 직접
재확인). `CHANGELOG_v1.6.md`와 fixture `README.md`의 표현을
5개로 정정. `test_legacy_fixture_structure.py`에 7-2번 검증
추가 — 전일 봉 포함 케이스 수가 정확히 5건인지 코드로 고정해,
향후 fixture가 바뀌어도 이 문서 오류가 재발하지 않도록 함.

### 2. "실제 입력 확정" 표현을 "사후 복원 입력창"으로 완화

GPT 지적을 실제 데이터로 재현 확인 — `실제_BUY` 케이스의 기록된
`current_vs_vwap_pct`(1.61%)를 복원된 60봉으로 직접 재계산하면
1.33%로, **차이가 실제로 존재**함을 확인. 이건 (1) 저장된 CSV가
당시 메모리 캐시의 정확한 스냅샷이 아니고 (2) 당시 캐시 갱신
시각·진행 중 봉 상태 등이 아직 미확인이라는 한계를 실증. fixture
`README.md`의 5번 섹션을 "legacy 실제 입력은 확정된다"는 어조에서
"저장된 분봉 기준 사후 복원 입력창(강한 정황 증거, 확정 아님)"
으로 전면 수정 — 재계산 오차 실측치(1.61% vs 1.33%)를 근거로
포함. 정확한 입력창 검증은 1B 진단 로그 도입 이후 가능하다는
점을 명시.

### 3~5. 테스트 검증 보강 (`test_legacy_fixture_structure.py`)

- **8번**: `compute_legacy_input()`이 암묵적으로 의존하던 전제
  (분봉 CSV가 `cntr_tm` 14자리 숫자 형식, timestamp 오름차순
  정렬, 중복 없음)를 명시적으로 검증하는 3건 추가 — 검증 없이
  `eligible[-60:]`을 쓰면 파일 순서가 바뀌었을 때 조용히 잘못된
  입력창을 계산할 위험이 있었음.
- **9번**: `expected_decisions.json`의 `legacy_requested_bar_count`
  가 fixture `settings.yaml`의 `market_regime.minute_bar_count`
  (정규식으로 추출, PyYAML 의존성 추가 없이)와 모든 케이스에서
  일치하는지 검증 — 설정이 바뀌었는데 expected가 안 바뀐 경우를
  잡기 위함.
- **10번**: `decision_timestamp`를 `YYYYMMDDHHMMSS`로 변환한 값이
  `bar_cutoff_timestamp`와 정확히 일치하는지 검증 — 수동 수정
  실수를 방지.

**검증**: 신규 검증 3건(9번 minute_bar_count 불일치, 10번
cutoff-timestamp 불일치)을 각각 합성 변조로 실제 FAIL 발생을
확인한 뒤 원상복구. 8번(정렬/중복) 로직은 별도 합성 데이터로
단위 검증(실제 fixture 파일은 훼손하지 않음).

**manifest 재생성 불필요**: 이번 라운드는 `expected_decisions.json`
내용 자체는 변경하지 않고(문서와 테스트 코드만 수정) 개수 표현
오류만 바로잡았으므로, 해당 파일의 SHA-256이 기존 manifest와
여전히 일치함을 확인 — 재생성하지 않음.

**전체 회귀**: `run_regression_tests.py` — 26개 파일 전부 통과,
종료코드 0. `test_legacy_fixture_structure.py` 단독 37건 전부
통과(30건 + 신규 7건).

---

## 1A.7: 문서 내부 모순 정정 + 테스트 견고성 보강 (2026-07-27)

**배경**: 1A.6 결과에 대한 GPT 3차 코드리뷰. 핵심 구현(fixture,
검증 로직)은 승인, 4가지 작은 보완만 요청.

### 1. CHANGELOG 내부 모순 정정

1A.5절에 "실제 데이터로 확정한 것"이라는 문구가 그대로 남아있어,
바로 다음 1A.6절의 "사후 복원이며 확정이 아니다"라는 정정과
같은 문서 안에서 모순되고 있었음(GPT 지적, 실제로 재검색해 확인
— fixture `README.md`는 지난 라운드에 이미 정확히 고쳤는데
`CHANGELOG_v1.6.md`의 원문 자체는 안 고쳤던 누락). 1A.5절 본문을
"저장된 누적 분봉을 cutoff로 잘라 사후 복원한 결과이며, 장 초반
입력창에 전일 봉이 포함됐을 가능성을 강하게 뒷받침한다. 당시
캐시의 정확한 입력은 1B 진단 도입 이후부터 확인할 수 있다"는
취지로 직접 수정.

### 2. README_배치안내.md 성격 확인

작업 디렉토리(`/home/claude/work/extracted_new`)에
`README_배치안내.md`가 존재하지 않음을 확인 — 이 파일은 매
라운드 diff zip을 만들 때 전달용으로만 별도 생성하는 임시
문서이며, 저장소에 커밋되는 파일이 아님. 혼동을 피하기 위해
git 커밋 대상에서 명시적으로 제외(아래 git 명령 참고).

### 3. YAML 파싱을 정규식에서 yaml.safe_load()로 교체

`test_legacy_fixture_structure.py`의 `minute_bar_count` 추출을
정규식(`re.search(r"minute_bar_count:\s*(\d+)", ...)`)에서
`yaml.safe_load()`(프로젝트에 이미 설치된 PyYAML 사용, 의존성
추가 없음)로 교체 — 정규식은 나중에 같은 키 이름이 주석이나 다른
섹션에 나타나면 잘못된 값을 잡을 위험이 있었음. `market_regime.
minute_bar_count` 경로로 정확히 접근하도록 수정, 환경변수
placeholder(`${...}`)가 `safe_load()` 단계에서도 단순 문자열로
안전하게 읽히는 것을 확인(`broker.app_key` 값으로 재현 확인).
더 이상 쓰지 않는 `re` import 제거.

### 4. 테스트 출력 번호 중복 해소

7-1/7-2 다음의 CSV 검증(8/9/10번) 이후, `runtime_state`/`settings`
검증이 다시 8/9번으로 출력되던 중복을 11/12번으로 정리 — 실패
로그를 볼 때 어느 8번인지 혼동되는 문제 해소.

**검증**: `legacy_requested_bar_count`를 999로 조용히 변조한 뒤
`yaml.safe_load()` 기반 검증이 여전히 정확히 FAIL을 잡아내는 것을
재확인(교체 전후 동작 동등성 확인) 후 원상복구.

**expected_decisions.json / manifest.sha256 변경 없음**: 이번
라운드는 문서와 테스트 코드만 수정 — 두 파일의 SHA-256이 이전과
정확히 동일함을 재확인.

**전체 회귀**: `run_regression_tests.py` — 26개 파일 전부 통과,
종료코드 0. `test_legacy_fixture_structure.py` 단독 37건 전부
통과(번호만 재정리, 검증 항목 수는 동일).

---

## 1B: raw_bars 진단 로깅 (2026-07-27)

**배경**: 1A 단계에서 fixture로 "저장된 CSV를 사후 복원하면 장
초반 케이스에 전일 봉이 섞였을 가능성이 강하다"는 정황 증거를
확보했지만, 이건 API 원본을 직접 관측한 게 아니었음. 1B는 처음
실제 운영 코드(`infra/broker/kiwoom_broker.py`)를 건드리는
단계 — GPT 지시대로 최우선 원칙은 **"기존 반환값을 byte-for-byte
로 완전히 보존하면서 관찰만 추가"**.

### 1. 진단 모델·순수 함수 분리

`infra/broker/minute_bar_diagnostics.py` 신규 — `MinuteBarDiagnostics`
dataclass와 `build_minute_bar_diagnostics()`(순수 함수, 로그 출력
없음), `format_diagnostics_log_line()`(로그 문자열 포맷팅만
담당)으로 명확히 분리. 진단 계산과 로그 출력을 한 함수에 섞지
않음(GPT 지시 1번).

### 2. 기존 반환값 byte-for-byte 보존 검증

`get_minute_bars()`의 핵심 로직(`raw_bars[:count]` 슬라이싱,
개별 `MinuteBar` 파싱, `bars.reverse()`, 반환 리스트)은 코드
한 글자도 바꾸지 않고 그대로 유지 — 진단은 반환 직전에 관찰만
하고 `bars` 값에 전혀 관여하지 않음. 진단 코드는 `try/except`로
감싸 `Exception` 발생 시 `warning` 로그만 남기고 기존 분봉 반환은
그대로 계속됨(fail-open, GPT 지시 2번).

**검증**: 진단 추가 이전의 순수 로직을 별도 함수(`legacy_parse`)
로 재현해, 합성 raw 응답 100개 → count 60 케이스에서 실제
`get_minute_bars()` 결과와 필드 단위(`cntr_tm`/`open_price`/
`high_price`/`low_price`/`close_price`/`volume`/`acc_volume`)로
완전히 동일함을 확인(테스트 1번). 진단 함수가 강제로 예외를
던지도록 만든 뒤에도 `get_minute_bars()`가 예외 없이 정상 완료
되고 반환값이 legacy와 동일함을 확인(테스트 8번, fail-open 검증).

### 3. 진단 최초 1회 키

`KiwoomBroker.__init__()`에 `self._minute_diagnostic_keys:
set[tuple[str, str, str, int]]` 추가. 키는 `(symbol, base_date,
tick_scope, count)` 조합(GPT 지시 3번 — 다른 tick_scope나 조회
개수를 쓰면 같은 종목·날짜라도 재진단해야 하므로). 로그 기록이
실제로 성공한 뒤에만 키를 추가 — 로그 자체가 실패해도 다음
호출에서 재시도 가능.

**검증**: 같은 조합으로 두 번 호출해도 키가 늘지 않음(중복 로그
방지, 테스트 9번), 다른 종목/다른 count는 각각 새 키를 만듦
(테스트 10번).

### 4. KST 시각 계산

`request_started_at`/`response_received_at`을
`datetime.now(ZoneInfo("Asia/Seoul"))`로 timezone-aware하게 기록
(GPT 지시 4번). API의 `cntr_tm`도 KST로 파싱한 뒤
`newest_raw_bar_age_seconds`(응답 시각과 최신 raw 봉의 시간차),
`newest_raw_bar_same_minute_as_response`(같은 분인지),
`newest_raw_bar_is_future`(age가 음수인 이상 상황 감지, GPT
추가 제안) 계산.

**검증**: 응답시각 09:16:45, 최신봉 09:16:00(완성봉 가정) 케이스에서
`age_seconds`가 정확히 45.0으로 계산되고 `same_minute=True`,
`is_future=False`임을 확인. 응답시각보다 미래인 봉을 합성해
`is_future=True`로 이상 감지되는 것도 확인(테스트 14, 15번).

### 5. parsed_count 분리

`raw_timestamp_parseable_count`(raw 전체 중 `cntr_tm` 파싱 가능한
개수)와 `returned_parsed_count`(count 제한 + 기존 파싱 규칙을
거쳐 실제 반환된 개수)를 명확히 분리(GPT 지시 5번, "하나의
`parsed_count` 이름으로 혼동하지 말 것"). 유효하지 않은
timestamp 2건을 섞은 합성 데이터로 `invalid_timestamp_count=2`,
`raw_timestamp_parseable_count = 전체 - invalid`가 정확히
계산됨을 확인(테스트 7번).

### 6. continuation 정보

`headers.get("cont-yn")`/`headers.get("next-key")`의 **원문은
저장하지 않고** `continuation_available: bool`/`next_key_present:
bool`만 기록(GPT 지시 6번). `KiwoomApiResponse.headers`가 이미
`cont-yn`/`next-key`를 파싱해서 갖고 있는 기존 구조를 그대로
재사용(`_to_api_response()`에서 이미 처리 중이던 것 확인).

**검증**: `next-key="abc123"`을 넣어도 진단 결과 객체와 로그
문자열 어디에도 그 원문이 노출되지 않음을 확인(테스트 4번).

### 7. 정규장 시간 기준

`regular_session_outside_count` 계산에 새 상수를 만들지 않고,
프로젝트에 이미 있던 `utils/time_utils.py`의
`MARKET_OPEN=time(9,0)`, `MARKET_CLOSE=time(15,20)`을 그대로
재사용(GPT 지시 7번, 임의로 새 기준을 정의하지 말 것). 다만 이
값이 "신규 매수/매도 중단 시각"(15:20)이지 실제 정규장 마감
(통상 15:30)과는 다른 의미라는 점을 코드 주석에 명시.

### 8. fixture 테스트 보강

`test_legacy_fixture_structure.py`의 `minute_bar_count` 검증에
타입·범위 확인 추가(GPT 지시 8번) — `int`인지, `bool`이 아닌지
(Python에서 `bool`은 `int`의 하위타입), `0`보다 큰지. 총 37→38건.

### 9. 실제 API 미확인 항목 명시

이 단계는 합성 응답으로만 검증했습니다. 다음은 **실제 API를
호출해야만 확인 가능**하며, 이번 라운드에서 추측하지 않았습니다
(GPT 지시 9번):

- `raw_received_count`의 실제 운영값(키움이 실제로 몇 개를 주는지 — 1A 단계 fixture 추정으로는 최소 63개 이상, 정확한 값 미확인)
- 최신 raw 봉이 실제로 완성봉인지 진행 중인 봉인지
- `continuation`(cont-yn/next-key)이 실제 응답에서 쓰이는지
- 실제 응답의 `raw_sort_direction`이 항상 DESC인지(1A fixture는
  저장 시점에 이미 reverse된 결과라 원본 순서를 알 수 없었음)

이 항목들은 **실운영 첫 호출에서 로그(`[MIN_BOOTSTRAP]`)로 확인
예정**입니다.

**전체 회귀**: `run_regression_tests.py` — `test_minute_bar_
diagnostics.py`가 자동으로 발견되어 총 27개 파일 전부 통과,
종료코드 0. `test_minute_bar_diagnostics.py` 단독 22건 전부 통과.

**실제 매매 판단 영향 없음 확인**: 진단 코드 어디에도
`session_metrics_mode`를 참조하는 곳이 없음(항상 무조건 관찰만
함) — `TradingService`는 `get_minute_bars()` 반환값을 그대로
사용하고 그 구조·타입이 전혀 안 바뀌었으므로, 기존 25개 회귀
테스트가 그대로 통과한 것 자체가 매매 로직 불변의 증거.

---

## 1B.1: 실운영 첫 로그 기반 진단 보강 (2026-07-27)

**배경**: 1B 배포 후 실제로 봇을 실행해 `[MIN_BOOTSTRAP]` 로그를
확인 — `raw_received=63`(요청 60인데 초과), `raw_order=UNKNOWN`
(정렬 방향 불명)이 실제로 관찰됨. GPT 코드리뷰로 두 가지 진단
공백을 지적받음: (1) 초과분을 사람이 로그를 눈으로 계산해야
알 수 있었음, (2) UNKNOWN이 왜 나왔는지 알 방법이 없었음.

### 1. raw_excess_count / raw_received_exceeds_requested 추가

`raw_received_count`와 `requested_count`를 나란히 로그에 찍기만
하던 것을, `raw_excess_count = raw_received_count - requested_
count`와 `raw_received_exceeds_requested: bool`로 명시적으로
계산·노출. 요청보다 3개 많이 온 실제 상황을 재현해 `raw_excess=3,
raw_exceeds_requested=True`가 정확히 계산되는지 확인, 대조군으로
raw가 요청보다 적은 경우(`raw_excess_count=-20`, 음수)도 확인.

### 2. raw_sort_direction=UNKNOWN 원인 진단

`raw_order_violation_count`(다수결로 정한 기준 방향에서 어긋나는
인접 쌍 개수), `raw_order_head_sample`/`raw_order_tail_sample`
(원본 순서 그대로 앞/뒤 5개 timestamp) 3개 필드 추가. 정렬이
정확히 2곳에서 스왑된 합성 데이터로 `raw_order_violation_count=2`
가 정확히 계산되는지, head/tail 샘플이 실제 값과 일치하는지 확인.

**2차 로그 분리**: 주 로그 한 줄에 원본 순서 전체를 넣으면 매번
로그가 길어지므로, `format_order_detail_log_line()` 신규 —
`raw_sort_direction`이 정확히 `UNKNOWN`일 때만
`[MIN_BOOTSTRAP_ORDER_DETAIL]` 2차 로그(`violations=N/전체` 비율
+ head/tail)를 남기고, ASC/DESC/N/A(정상)면 `None`을 반환해 로그를
안 남김. `kiwoom_broker.py`의 호출부에서 `None`이 아닐 때만
`warning` 레벨로 출력하도록 연결. 정상 정렬 케이스는 2차 로그가
생성되지 않음을 대조군으로 확인.

**여전히 미확인(다음 실운영 로그로 확인 예정)**: `raw_order_
violation_count`가 실제로 몇 건인지, 초과 3개가 매번 같은 패턴
인지(우연인지 구조적인지), UNKNOWN이 특정 종목·시간대에서만
발생하는지 — 이번 라운드는 "진단이 이 정보를 정확히 드러내는가"
까지만 검증했고, 실제 값 자체는 추측하지 않음.

**검증**: `test_minute_bar_diagnostics.py`에 5개 신규 검증 섹션
(17~21번) 추가, 총 22→34건. 신규 필드 추가가 1번 테스트(byte-
for-byte 반환값 불변)에 영향 없음을 재확인.

**전체 회귀**: `run_regression_tests.py` — 27개 파일 전부 통과,
종료코드 0.

---

## 1B.2: tzdata 미설치 환경 크래시 긴급 수정 (2026-07-27)

**배경**: 민우님이 실제 Windows 환경에서 `python test_minute_bar_
diagnostics.py`를 실행하자 `ModuleNotFoundError: No module named
'tzdata'`로 즉시 크래시. Windows는 macOS/Linux와 달리 OS 차원의
IANA 시간대 데이터베이스가 없어서, `zoneinfo.ZoneInfo("Asia/
Seoul")`을 쓰려면 `tzdata` pip 패키지가 반드시 설치되어 있어야
함 — 개발 환경(Linux 컨테이너)엔 시스템 tzdata가 있어서 이
문제를 미처 발견하지 못했음.

**근본 원인 — 1B 설계 원칙(fail-open)을 직접 어긴 지점 발견**:
`KST = ZoneInfo("Asia/Seoul")`이 `minute_bar_diagnostics.py`
모듈 **최상단**(import 시점에 즉시 평가)에 있어서, 이 줄에서
예외가 나면 모듈 import 자체가 실패함 — 게다가 `get_minute_bars()`
안에서 요청/응답 시각을 기록하던 두 지점(`_request_started_at`,
`_response_received_at`)도 `_maybe_log_minute_bar_diagnostics()`
를 감싸는 `try/except`보다 **앞**에 있어서, 이 예외가 fail-open
보호막 완전히 밖에서 **분봉 조회 자체를 실패**시킬 수 있는
구조였음. "진단 실패가 분봉 조회를 막으면 안 된다"는 1B의 핵심
원칙을 시각 기록 지점에서 놓친 설계 결함.

**수정**:
1. `KST`를 `zoneinfo.ZoneInfo("Asia/Seoul")`에서 `datetime.
   timezone(timedelta(hours=9), name="Asia/Seoul")`(고정 UTC+9
   오프셋)로 교체 — 한국 표준시는 서머타임이 없는 고정 오프셋이라
   IANA 시간대 데이터베이스 없이도 정확히 동일한 결과를 얻을 수
   있음. 실제 `ZoneInfo("Asia/Seoul")` 값과 비교해 오차가
   0.001초 미만(호출 시점 차이뿐)임을 확인. 이제 `tzdata` 패키지
   의존성 자체가 제거됨(`requirements.txt`에 추가 안 함 — 애초에
   불필요해짐).
2. `get_minute_bars()`의 두 시각 기록 지점을 각각 개별
   `try/except`로 감싸고 `minute_bar_diagnostics.KST`(고정
   오프셋)를 재사용 — 혹시 모를 다른 예외 상황에서도 시각 기록만
   `None`으로 남기고 분봉 조회는 계속됨.

**검증**: `builtins.__import__`를 패치해 `minute_bar_diagnostics`
모듈 관련 import 전체가 `ModuleNotFoundError`를 던지도록
강제하는 최악의 시나리오(실제 사용자가 겪은 상황과 동일한 종류)
로 재현 — 진단 모듈이 완전히 로드 불가능한 상태에서도
`get_minute_bars()`가 정상적으로 60개 분봉을 반환하고, 그 내용이
legacy 로직과 완전히 동일함을 확인(테스트 22번). `KST` 상수가
더 이상 `zoneinfo.ZoneInfo` 인스턴스가 아니라 `datetime.timezone`
고정 오프셋이며 정확히 UTC+9임을 직접 검증.

**전체 회귀**: `test_minute_bar_diagnostics.py` — 34→39건 전부
통과. `run_regression_tests.py` — 27개 파일 전부 통과, 종료코드 0.

---

## 1B.3: 테스트 파일에 남아있던 ZoneInfo 잔존분 수정 (2026-07-27)

**배경**: 1B.2에서 운영 코드(`minute_bar_diagnostics.py`,
`kiwoom_broker.py`)의 `ZoneInfo` 의존성은 제거했는데,
`test_minute_bar_diagnostics.py`를 실행한 민우님이 동일한
`ZoneInfoNotFoundError`를 다시 겪음 — 이번엔 운영 코드가 아니라
**테스트 파일 37번째 줄** 자체가 `from zoneinfo import ZoneInfo`
+ `KST = ZoneInfo("Asia/Seoul")`을 독립적으로 다시 정의하고
있었던 것을 놓쳤음(아이러니하게도 "KST가 더 이상 zoneinfo에
의존하지 않는지 확인하는 테스트"를 그 파일 안에 작성하면서, 정작
파일 상단 자체는 옛 방식을 그대로 남겨뒀던 실수).

**수정**: 테스트 파일에서 `from zoneinfo import ZoneInfo`와 자체
`KST` 정의를 제거하고, `infra.broker.minute_bar_diagnostics`가
이미 export하는 `KST`(1B.2에서 고정 UTC+9 오프셋으로 교체된 것)
를 그대로 import해서 재사용하도록 변경 — 앞으로 운영 모듈의
`KST` 정의가 바뀌어도 테스트가 자동으로 동기화되어 같은 종류의
불일치가 재발하지 않음. 파일 후반부에 중복으로 남아있던 `KST`
재import도 정리.

**전수 검사**: 프로젝트 전체(`grep -rln "ZoneInfo("`)에서 실제
`ZoneInfo(...)` 호출이 있는지 재확인 — `test_minute_bar_
diagnostics.py`와 `minute_bar_diagnostics.py` 두 파일에 문자열이
검색되지만, AST 파싱과 정밀 필터링으로 둘 다 **주석에만** 남아있고
실제 코드에는 `ZoneInfo(...)` 호출이 완전히 없음을 확인. `import
zoneinfo`(모듈 자체 import, `tzdata` 없이도 항상 성공)와
`ZoneInfo(key)`(실제 시간대 데이터 조회, `tzdata` 필요)를 구분해서
후자만 문제가 됨을 재확인.

**전체 회귀**: `test_minute_bar_diagnostics.py` — 39건 전부 통과.
`run_regression_tests.py` — 27개 파일 전부 통과, 종료코드 0.

---

## 1B.4: 진단 정밀도 보강 (2026-07-27)

**배경**: 1B.3까지의 결과를 GPT가 재검토 — 운영 코드 방향(고정
UTC+9, fail-open)은 승인하되, 1B 완료로 보기엔 이르다며 8가지
보완을 요청. `test_minute_bar_diagnostics.py` 상단 `ZoneInfo`
잔존 지적은 이번 세션 코드에서는 이미 1B.3에서 제거되어 있었음을
재확인(민우님 로컬 반영 시점 차이로 추정) — 나머지 7가지를 실제로
검증하고 반영.

### 1. _infer_sort_direction 얕은 형식검사 버그 (가장 중요)

GPT가 제시한 정확한 재현 케이스로 검증: `["20260721091600",
"20260230120000"(2월 30일, 존재하지 않는 날짜), "20260721091400"]`
— 기존 코드는 `_parse_bar_timestamp()`가 실제로 성공하는지 확인
안 하고 "14자리 숫자인가"라는 얕은 검사만 해서, 무효한 날짜가
문자열 비교에 섞여 `raw_sort_direction=UNKNOWN`으로 잘못 판정
(재현 확인). `_infer_sort_direction()`/`_count_order_violations()`
시그니처를 `(원본 timestamp, 파싱된 datetime)` 튜플 리스트로
변경해 파싱 성공 여부를 명시적으로 반영 — 수정 후 동일 케이스가
`invalid_timestamp_count=1`, `raw_sort_direction=DESC`(GPT 기대값과
정확히 일치)로 계산됨을 확인. `build_minute_bar_diagnostics()`
에서 이미 계산해둔 `parsed_dt_pairs`를 그대로 재사용하도록 해서
중복 파싱과 판단 기준 불일치를 함께 제거.

### 2. 로그 필드 확장

`format_diagnostics_log_line()`에 `request_started_at`/`response_
received_at`(ISO 8601 +09:00, 값 없으면 `N/A`), `request_duration_
ms`, `returned_oldest_timestamp`/`returned_newest_timestamp` 추가.
`_format_iso_or_na()` 헬퍼로 포맷 통일.

### 3. (위 1번과 통합 처리)

### 4. 빈 raw_bars에서도 진단 우선 실행

기존엔 `if not raw_bars: return []`이 진단 로직보다 먼저 실행돼
빈 응답의 요청/응답 시각·`raw_received=0`·continuation 여부를
전혀 확인할 수 없었음(재현 확인). 진단을 먼저 남기도록 순서
변경 — 기존 반환값(빈 리스트)과 에러 로그는 완전히 동일하게 보존.
**보류한 부분**: 빈 응답 후 같은 조합으로 재시도해 정상 응답이
오면, 최초 1회 키가 이미 등록되어 있어 두 번째 진단이 안 남는
설계 — 이건 이번 지시 범위를 벗어나는 별도 설계 문제(빈 응답과
정상 응답을 다르게 취급할지)라 확장하지 않고 현재 동작을 유지.

### 5. MARKET_CLOSE(전략 거래창)와 정규장 마감 분리

프로젝트 전체 검색 결과 15:30(정규장 마감)을 나타내는 기존
상수가 없음을 확인 후, 진단 전용 상수 `REGULAR_MARKET_CLOSE =
time(15, 30)`을 신설(근거: 한국거래소 정규장 공식 운영시간
09:00~15:30 — 이 상수는 매매 로직에 전혀 쓰이지 않고 오직 진단
목적으로만 사용). `regular_session_outside_count` 단일 필드를
`outside_strategy_window_count`(09:00~15:20 밖)와 `outside_
regular_market_count`(09:00~15:30 밖)로 분리. 15:25(전략거래창
밖, 정규장 안)를 포함한 합성 데이터로 두 값이 서로 다르게
계산됨(2건 vs 1건)을 확인.

### 6. legacy_parse가 실제 _parse_abs_int 규칙을 재현하지 않던 문제

기존 `legacy_parse()`가 단순 `int(item.get(...))`만 사용해, 실제
`_parse_abs_int()`의 특수 규칙(`None`/빈문자열→0, 음수/`+`부호는
절대값, 0 패딩, 잘못된 문자열→0)이 검증에서 전혀 드러나지 않고
있었음(합성 데이터가 전부 깨끗한 양수 문자열이었기 때문). 실제
구현을 그대로 복제한 `_legacy_parse_abs_int()`로 교체하고, 7가지
엣지케이스(정상/음수/`+`부호/0패딩/빈문자열/`None`/잘못된문자열)
로 `MinuteBar` 리스트 전체 equality를 검증.

### 7. 테스트 22번 설명 정정

"Windows tzdata 부재 완전 재현"이라는 설명이 부정확하다는 지적을
받아들여, 이 테스트가 실제로 검증하는 것("진단 모듈 동적 import
실패 시 broker fail-open")과 검증하지 않는 것("실제 Windows에서
tzdata 미설치 시 무슨 일이 일어나는가")을 명확히 구분해 주석과
변수명(`result_no_tzdata` → `result_diag_import_failed`)을 정정.
KST가 zoneinfo에 의존하지 않는지는 별도 검증 항목으로 유지.

### 8. get_minute_bars 시각 기록 중복 정리

요청 시작/응답 수신 두 지점에서 동일한 `try/except` 패턴이
중복되어 있던 것을 `KiwoomBroker._safe_diagnostic_now()`
정적 메서드로 정리 — 예외가 나도 `None`을 반환할 뿐 절대 던지지
않으므로 분봉 반환 로직에 영향 없음.

**검증**: `test_minute_bar_diagnostics.py` — GPT의 정확한 재현
케이스를 23번 테스트로 추가(`invalid=1, raw_sort_direction=DESC`
정확히 확인), 로그 필드 확장을 24번 테스트로 추가(ISO8601 형식,
`N/A` 대조군 포함), 빈 응답 진단을 10-1번 테스트로 추가, 엣지
케이스 7건을 1-1번 테스트로 추가. 총 39→62건 전부 통과.

**전체 회귀**: `run_regression_tests.py` — 27개 파일 전부 통과,
종료코드 0.

**실제 로그 예시**:
```
[MIN_BOOTSTRAP] symbol=475150 date=20260721 tick_scope=1 requested=60 raw_received=2 raw_excess=-58 raw_exceeds_requested=False raw_parseable=2 returned=2 request_started_at=2026-07-21T09:16:45.100000+09:00 response_received_at=2026-07-21T09:16:45.350000+09:00 request_duration_ms=250.0 oldest_raw=20260720143900 newest_raw=20260721091600 returned_oldest=20260720143900 returned_newest=20260721091600 raw_order=DESC raw_order_violations=0 returned_order=ASC continuation=False next_key_present=False other_date=1 outside_strategy_window=0 outside_regular_market=0 duplicates=0 invalid_ts=0 newest_bar_age_sec=45.4 same_minute_as_response=True is_future=False
```

**⚠️ 여전히 필요한 것**: 이 CHANGELOG는 합성 데이터 검증까지만
반영합니다. GPT가 요청한 "Windows에서 직접 `python test_minute_
bar_diagnostics.py`와 `python run_regression_tests.py`를 실행한
결과, 실제 `[MIN_BOOTSTRAP]` 한 줄"은 민우님이 로컬에서 직접
확인해주셔야 하는 부분으로 남아있습니다 — 이게 확인되어야 1C로
진행합니다.

---

## 1B.5: 진단 로그 무효화 버그 수정 (2026-07-27, 긴급)

**배경**: 1B.4 결과를 GPT가 3차 재검토하며 진단 시스템 전체를
사실상 무효화하는 심각한 버그를 발견 — 정상 raw 70개/count 60
케이스를 실제로 로그 캡처까지 해서 재현 제시.

**버그 내용**: `get_minute_bars()`가 `raw_bars`를 받은 직후, 응답이
정상이든 비어있든 상관없이 항상 먼저 `_maybe_log_minute_bar_
diagnostics(raw_bars=raw_bars, returned_bars=[])`를 호출해 키를
등록했음. 이후 실제 파싱·`reverse()`를 완료한 뒤 `returned_bars=
bars`로 다시 같은 함수를 불러도, 이미 등록된 (symbol, base_date,
tick_scope, count) 키 때문에 최초 1회 방어 로직이 이 두 번째(진짜
값을 담은) 호출을 조용히 스킵했음.

**실제 재현 결과** (수정 전): 함수는 정상적으로 60개를 반환하는데,
로그엔 `returned=0`, `returned_oldest=None`, `returned_newest=
None`, `returned_order=N/A`로 찍혀 진단 로그가 완전히 거짓 정보를
담고 있었음.

**수정**:

1. **`MinuteBarDiagnostics`에 `response_outcome`("EMPTY"|"SUCCESS")
   필드 추가**, `format_diagnostics_log_line()`에도 `outcome=`으로
   노출.
2. **`get_minute_bars()`를 완전히 재구성** — 빈 응답 진단은
   `if not raw_bars:` 분기 **안에서만** `response_outcome="EMPTY"`
   로 호출, 정상 응답 진단은 파싱·`reverse()` 완료 후 **단 한
   번만** `response_outcome="SUCCESS"`로 호출. 두 경로가 물리적으로
   분리되어 서로를 밀어낼 수 없는 구조로 변경.
3. **진단 키에 outcome 추가**: `(symbol, base_date, tick_scope,
   requested_count, response_outcome)` 5-튜플로 확장 — EMPTY와
   SUCCESS가 항상 서로 다른 키를 가져 독립적으로 기록됨.
4. **`_maybe_log_minute_bar_diagnostics()` → `_try_log_minute_bar_
   diagnostics()`로 개명**, `response_outcome` 파라미터 추가. 두
   군데 중복되어 있던 `try/except` 호출부는 그대로 유지(이 함수
   자체는 방어 로직을 추가하지 않고 예외를 그대로 전파 — fail-open
   책임은 여전히 호출부가 짐).

**재현 검증**: 버그 리포트와 정확히 동일한 시나리오(raw 70개,
count 60)로 재검증한 결과, 수정 후 로그가 `outcome=SUCCESS
returned=60 returned_oldest=20260721091100 returned_newest=
20260721101000 returned_order=ASC`로 정확하게 기록됨을 확인.

**추가 수정 (같은 라운드, GPT 지시 6·7번)**:
- `format_order_detail_log_line()`의 violations 분모를
  `raw_received_count - 1`에서 `max(raw_timestamp_parseable_
  count - 1, 0)`로 수정 — invalid timestamp가 섞이면 실제 정렬
  검사 대상보다 분모가 커져 비율이 부정확했던 문제(합성 데이터로
  분모 4→2 정확한 계산 확인).
- `returned_timestamp_parseable_count`/`returned_invalid_
  timestamp_count` 필드 추가 — raw 쪽에 이미 있던 대칭 필드를
  returned 쪽에도 추가.

**검증**: `test_minute_bar_diagnostics.py`에 GPT가 요구한 5가지
필수 테스트를 25-1~25-5번으로 추가 — (1) 정상 raw70/count60 시
`logger.info` 실제 호출을 캡처해 1회만 기록되고 `outcome=SUCCESS,
returned=60, returned_order=ASC, oldest/newest not None`을 확인,
(2) 동일 정상 키 재호출 시 SUCCESS 로그가 1회만 유지되는지, (3)
빈 응답 후 정상 응답 시 EMPTY 1회 + SUCCESS 1회로 정확히 분리
기록되는지, (4) 첫 `logger.info` 자체가 예외를 던져도 분봉은
정상 반환되고 키는 미등록되어 다음 호출에서 재시도되는지(fail-open
+ 재시도 가능성 동시 검증), (5) 이 모든 수정 이후에도 정상 응답
결과가 legacy와 byte-for-byte 완전히 동일한지. 26번(분모 수정),
27번(returned 진단 분리)도 추가. 총 62→83건 전부 통과.

이전 라운드까지는 `build_minute_bar_diagnostics()`를 직접 호출하는
단위 테스트만 있었는데, 이 버그는 `get_minute_bars()` 전체를
통합 실행해서 `logger.info` 실제 출력을 캡처해야만 잡을 수 있는
유형이었음 — 단위 테스트만으로는 발견 불가능했다는 점을 기록.

**전체 회귀**: `run_regression_tests.py` — 27개 파일 전부 통과,
종료코드 0.

**최종 확인 (GPT 요구 조건)**:
```
[MIN_BOOTSTRAP] symbol=475150 date=20260727 tick_scope=3 outcome=SUCCESS requested=60 raw_received=70 raw_excess=10 raw_exceeds_requested=True raw_parseable=70 returned=60 returned_parseable=60 returned_invalid=0 request_started_at=2026-07-27T15:20:23.375720+09:00 response_received_at=2026-07-27T15:20:23.375772+09:00 request_duration_ms=0.1 oldest_raw=20260721090100 newest_raw=20260721101000 returned_oldest=20260721091100 returned_newest=20260721101000 raw_order=DESC raw_order_violations=0 returned_order=ASC continuation=False next_key_present=False other_date=70 outside_strategy_window=0 outside_regular_market=0 duplicates=0 invalid_ts=0 newest_bar_age_sec=537023.4 same_minute_as_response=False is_future=False
```
`returned=60`, `returned_oldest=`/`returned_newest=`(실제 값),
`returned_order=ASC` 전부 정확히 출력됨을 확인 — GPT가 요구한
1B 완료 조건 충족(합성 데이터 기준).

**⚠️ 여전히 필요한 것**: 위 최종 확인은 합성 데이터 기준입니다.
Windows 실환경에서 `python test_minute_bar_diagnostics.py`,
`python run_regression_tests.py` 직접 실행과 실제 `[MIN_BOOTSTRAP]`
로그 확인은 여전히 민우님이 해주셔야 합니다.

---

## 1B.6: 분봉 조회 실패 시 오래된 캐시 기반 신규매수 차단 (2026-07-27, 안전 긴급)

**배경**: 1B.5 결과에 대한 GPT 재검토에서, 1B 진단 로깅과는 별개로
`TradingService._get_minute_analysis()`가 `get_minute_bars()` 실패
시 오래된 캐시를 그대로 분석에 넘기는 구조적 위험을 지적. 이건
실운영에서 분봉 조회 실패 직후 오래된 캐시 기반 신규 매수(039980)
가 발생했던 것과 같은 유형의 문제이며, 이번 1B 라운드 전체에서
한 번도 손대지 않은 별개의 코드 경로였음(확인: 기존
`except Exception: bars = self.cached_minute_bars.get(symbol, [])`
가 신선도 정보 없이 그대로 정상 `MinuteAnalysis`를 반환).

**수정**:

1. **`_get_minute_analysis()` 반환 타입 변경**: 기존
   `MinuteAnalysis | None` → `tuple[MinuteAnalysis | None, bool, str]`
   (`analysis, is_fresh, reason`). 정상 조회 성공 시 `is_fresh=True`,
   조회 실패했지만 캐시가 있어 그걸로 대체한 경우 `is_fresh=False,
   reason="STALE_MINUTE_DATA"`, 캐시조차 없는 경우
   `reason="MINUTE_DATA_UNAVAILABLE"`. **analysis 자체는 기존과
   동일하게 캐시 기반으로 계속 계산됨** — 보유 종목의 손절/트레일링
   판단이 끊기지 않도록.
2. **`_process_symbol()`에 미보유 종목 전용 안전장치 추가**:
   `position is None`(미보유)이고 `minute_data_fresh=False`인데
   전략이 `BUY` 신호를 냈다면, 신호 생성 직후 로그 기록 이전에
   `Signal(type=SignalType.HOLD, reason=minute_data_stale_reason)`
   으로 강제 전환. **보유 종목(`position is not None`)은 이 검사와
   완전히 무관하게 그대로 동작** — 위험 축소 SELL을 막으면 안
   되므로.

**검증**: 신규 `test_stale_minute_data_safety.py` — 13건 전부 통과.
(1) fresh/stale(캐시있음)/unavailable(캐시없음) 3가지 반환값 케이스
확인, (2) `_process_symbol()` 전체 흐름에서 미보유+stale+BUY신호가
실제로 `signal_log.csv`에 `HOLD`(BUY 아님)로 강제 전환되어 기록됨을
확인(핵심 안전장치 검증), (3) 보유종목+stale+SELL신호는 `_try_sell`
이 그대로 호출됨을 확인(위험축소 미차단), (4) 미보유+fresh+BUY신호
는 `_try_buy`가 정상 호출됨을 확인(회귀 없음).

**추가 정리 (GPT 지시 1번)**: `test_minute_bar_diagnostics.py`
10-1번의 진단 키 검증이 구형 4요소 키(`("475150", "20260721",
"3", 60)`)로 매칭을 시도하면서 `or len(...) == 1`이 붙어있어,
실제 키가 5요소(`outcome` 포함)로 바뀐 뒤에도 이 4요소 매칭은
항상 실패하지만 `or` 조건 덕분에 테스트가 계속 통과하던 상태였음
(정확한 키 구조를 검증 못 하고 있었음, 재현 확인). 정확한 5요소
키(`outcome="EMPTY"`) 매칭으로 수정.

**전체 회귀 확인 중 발견한 별개 문제 (범위 외, 미수정)**:
`run_regression_tests.py` 실행 중 `test_order_block_reason.py`와
`test_sold_today_qty_based.py` 일부가 실패 — 원인을 직접
재현·확인한 결과, **이번 수정과 무관한 기존 테스트의 시각
의존성 결함**이었음. 두 테스트가 `TradingService._try_buy()`를
실제로 호출하는데, `_try_buy()` 내부의 "14:50 이후 신규매수
차단" 로직이 `datetime.now()`(타임존 미지정, 시스템 로컬시각)를
그대로 써서, 이번 검증을 실행한 컨테이너의 로컬 시각이 우연히
23시대(UTC)였던 탓에 "14시 50분 이후"로 오판정됨. `datetime.now`
를 10:00으로 고정한 뒤 재실행하면 두 테스트 모두 정상 통과함을
직접 확인 — 즉 이번 1B.6 변경으로 인한 회귀가 아니라, 이전부터
있었지만 지금까지 실행 시각이 우연히 이 구간을 안 지나서 드러나지
않았던 별개의 취약점. 이번 라운드 범위(stale 캐시 안전장치)를
벗어나므로 수정하지 않고 별도 이슈로 명확히 기록만 남김 — 향후
`_try_buy()`의 시간 게이트를 테스트 가능하게 만들려면(예: 현재
시각을 주입받는 구조로 변경) 별도 라운드가 필요.

**전체 회귀**: `run_regression_tests.py` — 28개 파일 중 26개 통과,
위에서 설명한 시각 의존성 문제 2건은 이번 변경과 무관함을 재현
검증으로 확인. `test_stale_minute_data_safety.py`(신규, 13건)와
관련된 다른 모든 테스트는 정상 통과.

---

## 1B.7: 빈 응답·과거 봉 우회 경로 차단 (2026-07-28, 안전 긴급 2단계)

**배경**: 1B.6 결과에 대한 GPT 3차 재검토에서, 예외 발생 경로만
막았을 뿐 두 가지 안전 우회 경로가 그대로 열려 있음을 실제 재현과
함께 지적받음.

### 발견한 두 우회 경로 (둘 다 재현 확인)

1. **빈 응답 우회**: `get_minute_bars()`가 예외 없이 빈 리스트
   `[]`를 "정상 반환"하면, 기존 코드는 `is_fresh=True`를 유지한
   채 **기존 정상 캐시까지 빈 리스트로 덮어쓰고** `loaded_at`도
   갱신했음. 재현 확인: 정상 캐시가 있는 상태에서 API가 빈 응답을
   주면 그 캐시가 사라지고 `is_fresh=True`인 채 `None`이 반환됨.
2. **과거 봉 우회**: API가 예외 없이 과거(전거래일) 분봉을 정상
   반환해도, "예외가 없었다"는 이유만으로 `is_fresh=True`였음.
   재현 확인: 7/21 봉 60개를 반환하도록 만들었더니 최신 봉이
   `20260721095900`(전거래일)인데도 신선하다고 판정됨.

### 수정: MinuteDataResult 명시적 결과 객체로 전면 재설계

기존 `(analysis, is_fresh, reason)` 튜플이 "is_fresh"의 실제 의미
(API 호출 성공 여부일 뿐 데이터 신선도가 아님)를 정확히 담지
못한다는 지적을 받아들여, `domain/market_regime/minute_analyzer.py`
에 `MinuteDataResult` dataclass 신설:

```python
@dataclass(frozen=True)
class MinuteDataResult:
    analysis: MinuteAnalysis | None
    entry_safe: bool       # 신규 진입 판단에 안전한지
    source: str             # LIVE | CACHE | EMPTY | LIVE_OLD_BAR | UNAVAILABLE
    reason: str
    latest_bar_timestamp: str | None
    age_seconds: float | None
```

`_get_minute_analysis()`를 이 객체를 반환하도록 재작성:

- **빈 응답 처리**: `if not new_bars:` 분기를 신설해, 기존 캐시와
  `loaded_at`을 절대 건드리지 않고, 캐시가 있으면 `source="CACHE"`
  /`STALE_MINUTE_DATA`, 없으면 `source="EMPTY"`/`MINUTE_DATA_
  UNAVAILABLE`로 처리.
- **과거 봉 처리**: API 호출이 성공해도, 반환된 최신 봉의
  `cntr_tm`을 파싱해 (1) 파싱 자체가 성공하는지, (2) 오늘 날짜인지,
  (3) age가 `minute_bar_max_age_seconds`(신규 설정, 기본 120초)
  이내인지 확인. 셋 중 하나라도 실패하면 `source="LIVE_OLD_BAR"`
  /`STALE_MINUTE_DATA`.
- `entry_safe = (source == "LIVE" and reason == "")`로 최종 판정 —
  둘 다 만족해야 신규 진입 가능.

`config/settings.py`/`settings.yaml`에 `minute_bar_max_age_seconds:
120` 신규 추가(1분봉 기준 보수적 기본값, 3분봉 등으로 바뀌면
비례 조정 필요하다는 점을 주석에 명시).

**재현 시나리오로 실제 수정 검증**: 두 우회 재현 코드를 그대로
다시 실행 — 빈 응답 케이스는 `entry_safe=False`, `source="CACHE"`,
기존 캐시 보존, `loaded_at` 미갱신 확인. 과거 봉 케이스는
`entry_safe=False`, `source="LIVE_OLD_BAR"` 확인. 정상 케이스
(MockBroker 기본 응답, 오늘 시각 기준)는 여전히 `entry_safe=True`
임을 확인(회귀 없음).

**호출부(`_process_symbol()`) 갱신**: `minute_data_fresh`/
`minute_data_stale_reason` 지역변수를 `minute_data_entry_safe`/
`minute_data_reason`으로 교체, `MinuteDataResult`의 필드를 그대로
사용하도록 수정. 미보유 종목 + `entry_safe=False` + BUY 신호 →
HOLD 강제 전환 로직 자체는 1B.6과 동일하게 유지(조건식만 갱신).

**`test_stale_minute_data_safety.py` 전면 재작성**: 27건 전부 통과
(기존 13건에서 확장). GPT가 제시한 12개 필수 테스트 중 이번
라운드 범위(1, 2, 6, 7, 11번 — 빈 응답/과거 봉/파싱실패/age초과/
회귀 케이스)를 포함:
1. 정상 조회 → `entry_safe=True`
2~3. 예외 발생(캐시 有/無) → `entry_safe=False`
4~5. **빈 응답(예외 아님, 캐시 有/無)** → `entry_safe=False`,
   기존 캐시·`loaded_at` 보존 검증
6. **과거 봉 정상 반환** → `entry_safe=False`, `source=LIVE_OLD_BAR`
7. 최신 봉 timestamp 파싱 실패 → `entry_safe=False`
8. 최신 봉 age 초과(120초) → `entry_safe=False`
9. 미보유+EMPTY응답+BUY신호 → `_process_symbol()` 통합 흐름에서
   실제로 HOLD 강제 전환됨을 `signal_log.csv` 검증
10. 보유+stale+SELL → 차단 없이 `_try_sell` 호출됨(이번 라운드는
    SELL 세분화 없이 기존 동작 유지 — 아래 참고)
11. 회귀: 미보유+fresh+BUY → `_try_buy` 정상 호출

**전체 회귀**: `run_regression_tests.py` — 28개 파일 전부 통과,
종료코드 0(GPT 지시 6번 — 이전 보고의 "27/27"을 정확한 파일 수
"28/28"로 정정).

### 이번 라운드에서 의도적으로 미루는 것 (GPT 지시 3, 4, 5번)

범위가 크고 서로 독립적인 작업이라, 명확히 분리해서 별도 라운드로
미룹니다 — 지금 한 번에 다 처리하면 각 변경의 영향 범위를 검증하기
어려워짐:

- **(GPT 3번) stale SELL 세분화**: 현재는 보유 종목의 모든
  `stale` SELL을 차단 없이 허용 — 고정 손절/강제청산 같은 가격
  기반 위험축소 SELL과, VWAP/MA 이탈 같은 분봉 지표 기반 SELL을
  구분하지 않음. `Signal`에 `requires_fresh_minute_data`나
  `exit_category` 필드를 추가하는 설계가 필요하며, 각 전략
  (`breakout_strategy.py`, `neutral_strategy.py`, `bottom_
  strategy.py` 등)의 SELL 신호 생성 지점을 전부 검토해야 하는
  큰 작업.
- **(GPT 4번) stale 데이터의 부수효과 상태 오염 방지**: 현재
  `minute_analysis`가 stale이어도 `[MIN]`/`V_FAIL` 로그, 거래대금
  부족 카운트, 감시종목 자동제외 카운트가 그대로 갱신됨 — stale
  일 때 이 카운터들의 갱신을 멈추고 `[MIN_STALE]` 로그만 남기는
  것으로 분리 필요.
- **(GPT 5번) 14:50 시간 게이트의 Clock 의존성 주입**: `_try_buy()`
  내부의 `datetime.now()`(타임존 미지정, 시스템 로컬시각)가 실제
  운영 환경(UTC 서버)에서 KST 기준 시각과 어긋날 수 있고, 테스트도
  실행 시각에 따라 flaky함(1B.6절에서 재현 확인). `TradingService`
  에 `_now()` 메서드 또는 `Clock` 의존성을 두고 KST 기준으로
  통일, 테스트에서 고정 시각을 주입하는 구조로 변경 필요 — 이건
  `_try_buy()` 전체와 관련 테스트(`test_order_block_reason.py`
  등)에 영향을 주는 별도 작업.

이 세 가지는 다음 라운드에서 순서대로 처리 예정.

---

## 1B.8: 캐시 재사용 경로 우회 + KST timezone 계산 오류 수정 (2026-07-28, 안전 긴급 3단계)

**배경**: 1B.7 결과에 대한 GPT 4차 재검토에서 두 가지 심각한 문제를
실제 재현과 함께 지적받음.

### 발견한 두 문제 (둘 다 재현 확인)

1. **캐시 재사용 경로 우회 (가장 심각)**: 1회차 호출에서 과거 봉
   으로 `entry_safe=False`가 정확히 나와도, 60초 캐시 구간 안의
   2회차 즉시 재호출은 신선도 재검증이 전혀 없이 `source`가
   초기값 `"LIVE"`로 남아 `entry_safe=True`가 되던 치명적 버그.
   재현 확인: 동일한 `latest_bar_timestamp`인데 1회차는 `False`,
   2회차는 `True`. 이건 1B.7에서 만든 "최초 호출 차단"이 사실상
   무의미해지는 우회였음 — 실운영에서는 폴링 주기(10초)가 캐시
   구간(60초)보다 짧아 대부분의 호출이 이 우회 경로를 탐.
2. **naive datetime과 KST 봉 timestamp의 잘못된 비교**: 기존
   `datetime.now()`가 시스템 로컬시각(서버가 UTC로 설정된 환경
   에서는 UTC 그대로)을 반환하는데, 키움 API의 분봉 timestamp는
   KST 기준 — 재현 확인: UTC 서버가 로컬시각 00:20을 가진 상태에서
   KST 09:20 봉을 받으면 `age_seconds=-32400`(-9시간)으로 계산
   되는데도 `entry_safe=True`로 판정됨(음수 age를 걸러내는 로직도
   없었음).

### 수정

**1. `utils/time_utils.py`에 KST 전용 헬퍼 신설**: `KST_TZ =
timezone(timedelta(hours=9), name="Asia/Seoul")`(1B.2절에서 이미
검증된 tzdata 비의존 고정 오프셋 방식 재사용), `now_kst()`(항상
정확한 KST 반환, 서버 로컬시각과 무관), `parse_kst_bar_timestamp()`
(분봉 timestamp를 KST timezone-aware로 파싱). 기존 `now_local()`/
`MARKET_OPEN`/`MARKET_CLOSE`(naive 기반, 14:50 게이트 등 다른
코드 경로에서 여전히 사용 중)는 이번 라운드에서 건드리지 않음 —
그걸 바꾸면 검증 범위가 지나치게 커짐(GPT 지적 5번의 14:50 게이트
자체는 여전히 다음 라운드로 분리).

**2. `_evaluate_bar_freshness()` 헬퍼로 신선도 검증 로직 통합**:
API 응답 직후 검증과 캐시 재사용 시 검증이 완전히 동일한 함수를
호출하도록 재구성 — 두 경로의 판정 기준이 어긋날 수 없는 구조로
변경. 판정 기준: 최신 봉 `cntr_tm` 파싱 성공, `age_seconds >= -5`
(미래 timestamp 방어, 약간의 시계 오차는 허용), `age_seconds <=
minute_bar_max_age_seconds`.

**3. `_get_minute_analysis()` 전면 재작성**:
- 캐시 재사용(`need_refresh=False`) 경로에서도 `_evaluate_bar_
  freshness()`를 호출해 매번 재검증 — `source`를 `"CACHE_FRESH"`
  /`"CACHE_STALE"`으로 명확히 구분(GPT 지적: "캐시라는 이유만으로
  LIVE로 취급하지 마라").
- 신선도 검증에 실패한 응답으로는 `cached_minute_bars`/`cached_
  minute_bars_loaded_at`을 절대 갱신하지 않음 — "성공 캐시 시각"
  이 실패한 시도로 오염되지 않도록.
- 새 API 응답의 최신 봉이 기존 캐시의 최신 봉보다 오래되면 기존
  (더 신선한) 캐시를 보호하고 덮어쓰지 않음.
- `cached_minute_bars_failed_at`(신규 딕셔너리)로 성공 캐시 시각과
  분리해서 실패/빈응답 시각을 추적, `minute_fetch_backoff_seconds`
  (신규 설정, 기본 20초) 동안은 재시도하지 않음 — 실패가 연속될
  때 매 폴링(10초)마다 API를 계속 두드리지 않도록(이전 HTTP 429
  재발 방지).

`config/settings.py`/`settings.yaml`에 `minute_fetch_backoff_
seconds: 20` 신규 추가.

### 재현 시나리오로 실제 수정 검증

두 우회를 정확히 재현했던 코드를 그대로 다시 실행:
- 캐시 우회: 1회차 `entry_safe=False`, 2회차(즉시 재호출)도
  `entry_safe=False`로 정확히 확인.
- KST age: UTC 서버 환경 재현(시스템 로컬시각 00:20, KST 봉
  09:20)에서 `age_seconds=0.0`으로 정확히 계산됨을 확인(수정
  전엔 -32400).
- refresh 구간(60초) 안이지만 `minute_bar_max_age_seconds`(테스트
  값 30초)를 넘긴 캐시는 `source="CACHE_STALE"`으로 정확히 차단.
- 새 응답이 기존 캐시보다 오래된 봉이면 기존 캐시가 보존됨.
- 미래 timestamp(`age_seconds < -5`)가 정확히 차단됨.
- 빈 응답이 3회 연속 즉시 호출돼도 실제 API 호출은 1회만 발생
  (백오프 확인).

### `test_stale_minute_data_safety.py` 확장 (27→44건)

GPT가 제시한 8가지 필수 테스트를 모두 반영 — 과거 봉 1회차/즉시
2회차 차단(12번, 핵심 버그 검증), 백오프 만료 후 재조회 시 재확인
(12-1번, 추가), fresh 응답 후 cache hit(13번), refresh 구간 안
이어도 max_age 초과 시 차단(14번), stale 응답이 최신 캐시를
덮어쓰지 않음(15번), UTC host/KST bar age 정상 계산(16번), 미래
timestamp 차단(17번), 빈 응답 반복 시 백오프(18번).

기존 1~3번 테스트가 `MockBroker.get_minute_bars()`의 naive-UTC
봉 생성 방식과 충돌하는 것을 발견(MockBroker가 `datetime.now()`
로 봉을 만드는데, 이 테스트 환경 자체가 UTC를 로컬시각으로 써서
그 결과가 실제 KST 관점에서 9시간 전 봉이 되어버림 — MockBroker의
한계이지 운영 코드 버그 아님) — 해당 테스트들은 KST 기준으로
명시적으로 신선한 봉을 구성해 주입하도록 수정.

**전체 회귀**: `run_regression_tests.py` — 28개 파일 전부 통과,
종료코드 0.

### 이번 라운드에서도 미루는 것 (GPT 지시 3, 4, 5번 — 1B.7절과 동일 사유)

이번 1B.8 라운드가 이미 매우 크고 위험도 높은 캐시 우회를 다뤘고,
검증에 상당한 시간이 들었습니다. 아래 세 가지는 여전히 범위가
크고 서로 독립적이라 다음 라운드로 순서대로 분리합니다:

- **stale SELL 세분화**: `Signal`에 `requires_fresh_minute_data`
  /`exit_category` 필드 추가, 각 전략의 SELL 신호 생성 지점 전체
  검토 필요.
- **stale 데이터의 부수효과 상태 오염 방지**: `[MIN]`/`V_FAIL`
  로그, 거래대금 부족 카운트, 감시종목 자동제외 카운트가 stale
  이어도 갱신되는 문제.
- **14:50 게이트 KST Clock 주입**: `_try_buy()`의 `datetime.now()`
  (naive)를 `now_kst()` 또는 `TradingService._now()`/`Clock`
  의존성으로 교체, `MARKET_OPEN`/`MARKET_CLOSE`를 포함한
  `utils/time_utils.py`의 naive 기반 함수들도 함께 정리 필요 —
  이번 1B.8에서 만든 `now_kst()`를 재사용할 수 있는 지점.

---

## 1B.9: 진입 품질 검증(개수·정렬·중복·analysis 존재) 추가 (2026-07-28, 안전 긴급 4단계)

**배경**: 1B.8 결과에 대한 GPT 5차 재검토에서, 신선도(age)만
확인하고 데이터의 실질적 품질은 전혀 확인하지 않던 새로운 우회를
실제 재현과 함께 지적받음.

### 발견한 문제 (재현 확인)

`_evaluate_bar_freshness()`가 "최신 봉 1개의 age"만 검사하고,
분봉 개수나 `MinuteAnalyzer` 분석 성공 여부는 전혀 검사하지
않았음. 재현: API가 최신 분봉 **1개만** 반환하도록 만들면
`MinuteDataResult(analysis=None, entry_safe=True, source="LIVE",
reason="")`이 나옴 — timestamp 하나가 신선하기만 하면
`entry_safe=True`가 되는데, `analysis`는 `None`(MinuteAnalyzer가
내부적으로 이미 최소 봉 수 미달로 `None`을 반환하고 있었음에도
호출부가 이를 무시). 60개 전부 동일 timestamp(중복)로 만들어도
같은 우회가 발생. GPT가 실제 `BreakoutStrategy`로 일봉 조건
4개를 충족시켜 `generate_signal()`을 호출하면 `SignalType.BUY`
(`"강한 진입 4/8"`)가 실제로 나옴을 확인했다고 보고받음 — 분봉
데이터가 사실상 없는데도 일봉 점수만으로 매수가 나갈 수 있는
경로였음.

### 수정

**1. `_evaluate_bar_freshness()`를 진입 품질 검증으로 확장**:
기존 age 검사에 다음을 추가(하나라도 실패하면 차단):
- 분봉 개수가 `minute_bar_min_count_for_entry`(신규 설정, 기본
  60 — `minute_bar_count`와 동일한 보수적 값) 이상
- 전체 봉의 `cntr_tm`이 모두 파싱 가능
- timestamp가 엄격한 오름차순(`a < b`, 중복이면 `a <= b`라
  실패 — 정확히 GPT 지시대로 중복도 차단)

**2. `entry_safe` 최종 조건에 `analysis is not None` 추가**:
```python
if analysis is None:
    entry_safe = False
    if reason == "":
        reason = "MINUTE_ANALYSIS_UNAVAILABLE"
else:
    entry_safe = source in ("LIVE", "CACHE_FRESH") and reason == ""
```
진입 품질 검증을 통과한 `source`/`reason`이어도 `MinuteAnalyzer.
analyze()`가 내부적으로 `None`을 반환하면(예: analyzer 자체의
다른 최소요건 미달) 무조건 차단.

**3. `REGRESSED_MINUTE_RESPONSE` 정책 확정**: 새 API 응답이
기존 캐시보다 오래됐지만 그 자체는 `max_age` 이내인 경우
(1B.8에서는 이 경우도 `source="CACHE_FRESH"`로 "정상 확인됨"처럼
보였음), GPT가 요청한 대로 **보수적으로 차단**하는 정책을 확정 —
`reason="REGRESSED_MINUTE_RESPONSE"`로 명시하고 `entry_safe=False`.
`analysis` 자체는 기존(더 신선한) 캐시로 계속 제공해 보유종목
판단은 유지.

이 함수는 진단(`infra/broker/minute_bar_diagnostics.py`의
`raw_order_violation_count` 등, 관찰 전용, 1B단계 원칙 유지)과는
별개로 **실제 진입을 막는 안전장치**입니다 — 같은 종류의 이상
(중복/비정렬/파싱실패)을 진단은 관찰만 하고, 이 함수는 신규
진입 차단에 실제로 반영합니다.

### 재현 시나리오로 실제 수정 검증

- 최신 분봉 1개만 반환 → `entry_safe=False`(수정 전 `True`였음)
- 60개 전부 동일 timestamp(중복) → `entry_safe=False`
- 정상 60개 오름차순 → 여전히 `entry_safe=True`(회귀 없음)
- 퇴행된 응답(기존 캐시보다 오래됨, 둘 다 max_age 이내) →
  `entry_safe=False`, `reason=REGRESSED_MINUTE_RESPONSE`

### `test_stale_minute_data_safety.py` 확장 (44→59건)

GPT가 제시한 필수 통합 테스트 6가지를 모두 반영 — 최신 분봉
1/10/59개(모두 최소 60 미만, 19번), 60개 정상(20번, 회귀 확인),
60개지만 중복 timestamp(21번), 60개지만 invalid timestamp
포함(22번), 60개지만 순서 뒤섞임(23번), `analysis=None`(24번,
`MinuteAnalyzer.analyze()`를 직접 patch해서 재현), 퇴행 응답
정책(25번), `_process_symbol()` 통합 흐름에서 진입품질 미달 +
BUY신호가 실제로 HOLD로 강제 전환되는지(26번, GPT가 재현한
"일봉 4점만으로 실제 BUY" 시나리오를 정확히 차단하는지 확인).

기존 16번(UTC/KST age 정확성) 테스트가 봉 1개로 구성되어 있어
새로 추가한 최소개수 검증에 걸리는 것을 발견 — 이 테스트의 본래
목적(age 계산 정확성)과 최소개수 검증은 서로 다른 관심사이므로,
60개로 채우되 마지막 봉만 정확히 목표 시각으로 맞춰 두 검증이
서로 간섭하지 않게 수정.

**전체 회귀**: `run_regression_tests.py` — 28개 파일 전부 통과,
종료코드 0.

### 이번 라운드에서도 미루는 것 (GPT 지시 5번 — 1B.7·1B.8절과 동일 사유로 계속 분리)

이번 1B.9도 이미 상당히 크고 위험도 높은 우회(진입 품질 전무)를
다뤘습니다. 아래 네 가지는 1B.7·1B.8에서 이미 두 차례 미뤄온
것과 동일 항목으로, 여전히 범위가 크고 서로 다른 코드 경로를
건드려야 해서 다음 라운드로 분리합니다:

- **stale 데이터의 부수효과 상태 오염 방지**: `[MIN]`/`V_FAIL`
  로그, 거래대금 부족 카운트, 감시종목 자동제외 카운트가 stale
  이어도 그대로 갱신되는 문제 — 아직 미착수.
- **stale SELL 세분화**: 가격 기반 hard-risk SELL(고정손절/
  최대손실/강제청산)만 허용하고 VWAP/MA 등 지표 기반 SELL은
  fresh 데이터 요구 — `Signal`에 `requires_fresh_minute_data`/
  `exit_category` 추가 및 각 전략의 SELL 신호 생성 지점 전체
  검토 필요, 아직 미착수.
- **14:50 게이트 KST Clock 주입**: `_try_buy()`의 `datetime.
  now()`(naive)를 `now_kst()` 기반으로 교체, 14:49:59/14:50:00
  경계 및 UTC 서버 테스트 추가 — 이번 1B.9에서 만든 `now_kst()`
  를 그대로 재사용 가능하나 아직 미착수.

**다음 세션 착수 순서 제안**: 위 세 가지가 이제 세 라운드째
연속으로 미뤄지고 있어, 다음 라운드는 이 중 하나만 골라 깊이
있게 처리하기보다 **세 가지를 한 번에 순서대로 전부 마무리**하는
것을 목표로 잡는 게 안전할 것 같습니다(GPT도 "1C 전에 모두
완료"를 조건으로 제시함) — 다만 각 항목은 이번처럼 재현 검증부터
시작해 별도 커밋으로 나눠 진행 예정.

---

## 1B.10: "1B Safety Closure" — 남은 안전 항목 전부 마감 (2026-07-28)

**배경**: 1B.9 결과에 대한 GPT 6차 재검토에서 새로운 우회(OHLC
구조 미검증)를 재현 지적받았고, 1B.7부터 세 라운드째 이월되던
항목들도 "다음 라운드는 더 이상 나누지 말고 한 번에 마감"하라는
지시를 받음. 이번 라운드에서 전부 처리.

### 1. OHLC 구조 품질 검증 (새로 발견된 우회, 재현 확인)

`_evaluate_bar_freshness()`가 신선도·개수·정렬만 확인하고 OHLC
구조 자체는 검증하지 않아서, `open=58000, high=0, low=0, close=
58000`인 60개 봉을 넣으면 신선도 검증은 통과(`fresh_ok=True`)하고
`MinuteAnalyzer.analyze()`에서 `day_high=0`으로 나누다가
`ZeroDivisionError`가 그대로 `_get_minute_analysis()` 밖까지
전파되던 문제를 재현 확인. 더 심각하게는 예외 발생 **전에** 이미
`cached_minute_bars`/`loaded_at`을 갱신하고 있어서, 잘못된 응답이
성공 캐시를 오염시키는 구조였음.

**수정**: `_evaluate_bar_freshness()`에 OHLC 검증 추가 —
open/high/low/close 모두 `> 0`, `low <= open <= high`, `low <=
close <= high`, `volume >= 0`, `acc_volume >= 0`을 전체 봉에 대해
검사. 위반 시 `reason="INVALID_MINUTE_OHLC"`로 명시. 반환 튜플을
`(fresh_ok, latest_dt, age_seconds, detail)` 4개에서
`(fresh_ok, latest_dt, age_seconds, reason_code, detail)` 5개로
확장해 명시적 reason 코드를 `MinuteDataResult.reason`에 그대로
전달.

**재작업 중 발견한 2차 버그**: OHLC 검증 블록을 삽입하며
`latest_ts`/`latest_dt`/`age_seconds` 계산 코드를 실수로 삭제해
`NameError`가 나던 것을 직접 재현 테스트로 발견·복구 — 정상
케이스(analyzer 예외 검증용 60개 정상 봉)를 테스트하다가
`NameError: name 'age_seconds' is not defined`가 나서 원인을
추적해 고쳤음.

### 2. `MinuteAnalyzer.analyze()` try/except 방어

호출부를 `try/except`로 감싸 예외가 `_process_symbol()`까지
전파되지 않도록 함 — 예외 시 `analysis=None`, `reason=
"MINUTE_ANALYSIS_ERROR"`로 안전하게 처리하고 해당 종목만 차단,
나머지 종목 처리 루프는 계속됨.

### 3. 캐시 반영 순서 변경 (검증 통과 후에만 갱신)

기존엔 `self.cached_minute_bars[symbol] = new_bars`와
`self._minute_saver.save(symbol, new_bars)`가 신선도/구조 검증
**이전**에 실행되고 있었음 — 이번에 순서를 뒤집어 구조 검증을
통과한 응답만 성공 캐시·정상 저장 파일에 반영되도록 재구성.

### 4. 거부 응답 진단 메타데이터 별도 경로 기록

`_save_rejected_minute_bars()` 신설 — 구조/신선도 검증에 실패한
응답의 진단 메타데이터(`reason`/`detail`/`bar_count`/`first_ts`/
`last_ts`)를 `{minute_bars_dir}/rejected/{symbol}_{날짜}.log`에
한 줄로 기록. 정상 리플레이용 CSV(`{minute_bars_dir}/{날짜}/
{symbol}.csv`)에는 검증 통과 데이터만 저장되어 거부 데이터가
조용히 섞이지 않음. 저장 자체는 fail-open(예외가 나도 진입 차단
로직에 영향 없음).

**⚠️ 표현 정정 (1B.11절)**: 최초 보고 시 "거부 응답을 별도 경로에
저장"이라고 표현했으나, 실제로는 분봉 데이터 전체(OHLCV)가 아니라
위 5개 필드로 구성된 메타데이터 한 줄만 기록함 — GPT 7차 코드
리뷰 지적으로 정정.

### 5. 1B.7부터 이월된 세 항목 전부 마무리

**(a) stale 상태 카운터/로그 오염 방지**: `[MIN]`/`[V_FAIL]` 로그와
`_low_volume_count`/자동제외 카운터 갱신을 `minute_data_entry_
safe`인 경우에만 실행하도록 제한. stale이면 `[MIN_STALE]` 관찰
로그만 남기고(60초 스로틀) 카운터는 그대로 유지(증가도 리셋도
안 함).

**(b) stale SELL 세분화**: `Signal`(`domain/models.py`)에
`requires_fresh_minute_data: bool = False` 필드 신설. 17곳의
SELL 신호 생성 지점 중, `breakout_strategy.py`/`neutral_
strategy.py`의 "추세 꺾임"(VWAP/MA5 이탈을 점수에 반영하는 지표
기반 SELL)에만 `True`로 표시. 나머지(고정 손절/트레일링 스탑/
안전망 익절은 가격만 사용, `swing_strategy`의 추세꺾임은 RSI/
MACD 같은 일봉 지표라 분봉 신선도와 무관, `bottom_strategy`/
`hold_strategy`는 전부 가격 기반)는 건드리지 않음 — 실제 코드를
읽고 각 SELL이 `minute_analysis`를 참조하는지 하나하나 확인한
결과. `_process_symbol()`에 `position is not None and signal.
type == SELL and requires_fresh_minute_data and not entry_safe`
조건으로 지표 기반 SELL만 HOLD로 전환하는 로직 추가 — 가격 기반
hard-risk SELL은 이 검사와 무관하게 계속 허용.

**(c) 14:50 게이트 KST Clock 전환**: `_try_buy()`의 `datetime.
now()`(naive)를 `now_kst()`(1B.8에서 신설한 tzdata 비의존 고정
UTC+9)로 교체.

### 6. 설정 검증 추가

`MarketRegimeConfig.__post_init__()`에 4가지 검증 추가 —
`minute_bar_count > 0`, `1 <= minute_bar_min_count_for_entry <=
minute_bar_count`, `minute_bar_max_age_seconds > 0`,
`minute_fetch_backoff_seconds >= 0`. 위반 시 `ValueError`로 설정
로드 시점에 즉시 실패 — 잘못된 조합으로 안전장치가 항상
차단되거나 항상 무력화되는 조용한 오설정을 방지.

### 7. 테스트 파일 분리

GPT 지시대로 관심사별로 신규 파일 2개 추가:
- `test_minute_ohlc_quality_safety.py`(신규, 26건) — OHLC 구조
  검증, analyzer 예외 방어, 캐시/저장 오염 방지.
- `test_stale_sell_and_clock_safety.py`(신규, 11건) — stale
  카운터 불변, SELL 세분화, 14:50 KST 경계·UTC 서버 검증.

기존 `test_stale_minute_data_safety.py`(59건)는 그대로 유지 —
캐시 재사용/신선도/진입 품질 검증 범위는 이미 정리되어 있어
추가 분리하지 않음.

### 재현 시나리오로 실제 수정 검증

- OHLC(`high=0, low=0`) → 예외 없이 `entry_safe=False`,
  `reason=INVALID_MINUTE_OHLC`, 캐시 완전 무오염
- `low > high`, `close` 범위 밖, `volume` 음수 → 전부 정확히 차단
- `MinuteAnalyzer.analyze()` 강제 예외 → `MINUTE_ANALYSIS_ERROR`로
  안전 차단, `_process_symbol()` 예외 없이 완료
- 거부 응답 → 정상 CSV 행 수 불변, `rejected/`에 사유 포함 기록
- stale + hard-risk SELL(손절) → `_try_sell` 정상 호출(허용)
- stale + indicator SELL(추세꺾임) → `_try_sell` 미호출(차단)
- 14:49:59/14:50:00/14:50:01 KST 경계 → 정확히 판정
- UTC 서버(로컬시각 UTC 05:50 = KST 14:50) → 정확히 `AFTER_1450`
  차단, UTC 05:49:59(KST 14:49:59)는 정확히 허용

**전체 회귀**: `run_regression_tests.py` — 30개 파일(기존 28 +
신규 2) 전부 통과, 종료코드 0.

### 이번 라운드로 GPT가 제시한 완료 기준 충족 현황

```
timestamp·개수 품질 검증: 완료 (1B.9)
캐시·KST 신선도 검증: 완료 (1B.8)
OHLC 데이터 품질 검증: 완료 (이번 1B.10)
분석 예외 fail-close: 완료 (이번 1B.10)
stale 상태 오염 차단: 완료 (이번 1B.10)
stale SELL 세분화: 완료 (이번 1B.10)
14:50 KST Clock: 완료 (이번 1B.10)
설정값 검증: 완료 (이번 1B.10)
Windows 실제 API 로그 확인: 미완료 — 사용자 확인 필요
```

**⚠️ 여전히 필요한 것**: 위 표의 마지막 항목만 남았습니다.
Windows 실환경에서 `[MIN_BOOTSTRAP] outcome=SUCCESS`,
`returned=60`, `returned_oldest`/`returned_newest`,
`returned_order=ASC` 로그를 직접 확인하는 것은 여전히 민우님이
해주셔야 합니다 — 이게 확인되면 1C(세션 지표 shadow 구현)로
넘어갈 수 있습니다.

---

## 1B.11: entry_watch의 stale VWAP 청산 경로 핫픽스 (2026-07-28)

**배경**: 1B.10 결과에 대한 GPT 7차 재검토에서, `_process_symbol()`
이 정규 `strategy.generate_signal()`보다 **먼저** 호출하는
`_check_entry_watch()` 경로가 stale SELL 세분화(1B.10에서 구현)의
사각지대에 있음을 재현과 함께 지적받음.

### 발견한 문제 (재현 확인)

`_check_entry_watch()`는 매수 후 `watch_minutes`(+1분 버퍼) 동안
정규 전략보다 먼저 실행되는데, 내부의 VWAP 이탈 청산이
`minute_analysis`를 직접 참조하면서도 `requires_fresh_minute_data`
표시가 없었고, `_process_symbol()`도 이 함수에 `minute_analysis`를
`entry_safe` 여부와 무관하게 그대로 전달하고 있었음. 1B.10에서
추가한 stale SELL 세분화 테스트(`test_stale_sell_and_clock_
safety.py` 3~5번)는 전부 `_check_entry_watch`를 `patch(return_
value=None)`로 비활성화하고 정규 전략의 SELL만 검증했기 때문에,
정규 전략보다 먼저 실행되는 이 경로 자체는 한 번도 검증되지
않았음.

재현: `entry_time_by_symbol`을 2분 전으로 설정, `MinuteDataResult
.entry_safe=False`(`CACHE_STALE`), stale `minute_analysis.
price_above_vwap=False`, 가격은 급락(`fail_cut_pct`) 조건에
걸리지 않는 수준(+0.2%)으로 구성한 뒤 `_check_entry_watch()`를
직접 호출 — `Signal(type=SELL, reason='entry_watch VWAP이탈청산
...', requires_fresh_minute_data=False)`와
`vwap_break_streak_by_symbol[symbol]=1`이 실제로 반환됨을 확인.

### 수정

**1. `_process_symbol()`에서 entry_watch에 전달하는 minute_
analysis를 entry_safe 여부로 조건부 처리**:
```python
entry_watch_minute_analysis = (
    minute_analysis if minute_data_entry_safe else None
)
if not minute_data_entry_safe:
    self.state.vwap_break_streak_by_symbol.pop(symbol, None)

signal = self._check_entry_watch(
    symbol, position, market_price.current_price, entry_watch_minute_analysis,
)
```
`_check_entry_watch()` 호출 자체는 그대로 유지(GPT 지시 3번 —
급락청산/시간초과청산 같은 가격·시간 기반 판단은 stale이어도
계속 필요하므로), VWAP 판단에만 쓰이는 인자를 stale이면 `None`
으로 넘겨서 함수 내부의 `if ew.fail_on_vwap_break and minute_
analysis is not None:` 조건 자체가 거짓이 되도록 함 — "판단 자체를
실행하지 않는" 구조(GPT 권장 방식).

**2. stale 구간 진입 시 `vwap_break_streak_by_symbol` 명시적
리셋**: GPT 권장대로 보수적으로 0(제거)으로 리셋 — stale 구간이
이전의 VWAP 연속 이탈 확인을 이어가지 않도록 함.

**3. VWAP 이탈 SELL Signal에 `requires_fresh_minute_data=True`
추가(2차 방어)**: 1번 수정으로 이미 stale 상황에서는 이 코드
지점에 도달하지 않지만, 방어적으로 표시를 추가.

**4. `_save_rejected_minute_bars()` 표현 정정**: docstring과
CHANGELOG 4번 섹션의 "거부 응답을 별도 경로에 저장"이라는 표현이
부정확하다는 지적을 받아들여 — 실제로는 분봉 데이터 전체가 아니라
`reason`/`detail`/`bar_count`/`first_ts`/`last_ts` 메타데이터
한 줄만 기록한다는 점을 명시하도록 정정.

### 재현 시나리오로 실제 수정 검증

GPT가 요구한 4가지 필수 시나리오를 정확히 재현:
- stale + VWAP 이탈 → `_try_sell` 미호출, `vwap_break_streak_
  by_symbol` 갱신 없음(`None` 유지)
- fresh + VWAP 이탈 → `_try_sell` 정상 호출(회귀 없음)
- stale + `fail_cut_pct` 급락 → `_try_sell` 호출(가격 기반이라
  허용)
- stale + `watch_minutes` 경과 후 최소수익 미달 → `_try_sell`
  호출(시간+가격 기반이라 허용)

**재현 과정에서 발견한 테스트 설계 문제**: `MockBroker`의 기본
가격(`475150`은 목록에 없어 `10000원`)과 테스트가 설정한
`average_price`(예: `58058원`)가 맞지 않아 `pnl_pct`가 의도치
않게 급락 조건에 먼저 걸리는 문제를 발견 — `broker._prices`를
명시적으로 고정하는 방식으로 정확한 시나리오를 구성하도록 수정.

### `test_stale_sell_and_clock_safety.py` 확장 (11→16건)

`make_minimal_minute_analysis()`가 `**overrides`를 받도록 확장,
`build_service()`에 `fixed_price` 옵션 추가. GPT가 명시적으로
요구한 대로 `_check_entry_watch`를 **patch하지 않은** 진짜
`_process_symbol()` 통합 테스트를 11번으로 추가 — 이게 이번
버그를 놓쳤던 근본 원인(entry_watch 경로 자체를 우회하고 있었던
것)을 직접 해소.

### 회귀 테스트 중 발견한 별개 문제 (제 변경으로 인한 회귀 아님, 확인 완료)

`run_regression_tests.py` 실행 중 `test_order_block_reason.py`
(4건)와 `test_sold_today_qty_based.py`(1건)가 실패 — 원인을
직접 재현·확인한 결과, 이번 라운드 변경과 무관한 **1B.6절에서
이미 발견하고 "범위 밖" 문제로 기록해뒀던 것과 정확히 같은
근본 원인**임을 확인:

두 테스트가 `TradingService._try_buy()`를 시각 고정 없이 직접
호출하는데, 1B.10에서 `_try_buy()`의 14:50 게이트를 `datetime.
now()`(naive)에서 `now_kst()`(정확한 KST 변환)로 교체한 결과,
이번엔 실제 KST 현재 시각(테스트 실행 시점 기준 15:21경, 장 마감
이후)이 정확하게 반영되어 두 테스트가 일관되게 `AFTER_1450`으로
차단됨. 이전엔 naive `datetime.now()`가 컨테이너의 UTC 로컬시각을
그대로 썼기 때문에 "우연히" 걸리거나 안 걸리는 flaky 상태였을
뿐 — 이번 수정으로 KST 게이트 자체는 오히려 정확해졌고, 문제는
두 테스트가 시각에 의존하는 설계 결함을 갖고 있었다는 점.

**재현 검증**: `now_kst()`를 장중 시각(10:00 KST)으로 고정한 뒤
두 테스트를 재실행하면 각각 5/5, 정상 통과함을 확인 — 이번 라운드
변경으로 인한 회귀가 아님을 명확히 함. 두 테스트 자체를 고정
시각을 주입받는 구조로 바꾸는 작업(1B.6절에서 이미 예고했던 항목)
은 이번 범위를 벗어나므로 착수하지 않음.

**전체 회귀**: `run_regression_tests.py` — 30개 파일 중 28개
통과, 2개(`test_order_block_reason.py`, `test_sold_today_qty_
based.py`)는 위에서 설명한 기존 시간대 의존성 문제로 인한 실패
(제 변경으로 인한 회귀 아님, 장중 시각 고정 시 정상 통과 확인).
`test_stale_sell_and_clock_safety.py`(16건, 신규 5건 포함)와
이번 라운드가 건드린 다른 모든 관련 테스트는 정상 통과.

---

## 1B.12: 시간대 의존 테스트 2건 수정 — 공식 회귀 30/30 확정 (2026-07-28)

**배경**: 1B.11 결과에 대한 GPT 8차 재검토에서, entry_watch 핫픽스
자체는 승인하되 "공식 회귀 30/30"이라는 표현이 부정확하다는 지적을
받음 — 실제로는 `test_order_block_reason.py`/`test_sold_today_
qty_based.py`가 실행 시각(KST 14:50 이후)에 따라 실패하는
28/30이며, 이걸 30/30으로 보고하면 안 된다는 지적. 1B.11에서
이미 원인은 규명했었으나(1B.10의 14:50 게이트 `now_kst()` 교체로
인해 두 테스트가 시각 고정 없이 `_try_buy()`를 호출하면 실제 KST
현재 시각이 정확히 반영되어 항상 차단됨) 수정은 다음 라운드로
미뤄뒀던 것을, 이번에 정리.

### 수정

**`test_order_block_reason.py`**: `_try_buy()` 호출 4곳
(최대보유종목수/RiskManager거부/ALREADY_HOLDING/BUY신호쿨다운
시나리오) 전부를 `with patch("domain.service.trading_service.
now_kst", return_value=FIXED_MARKET_TIME):`(장중 KST 10:00 고정)
로 감싸도록 수정.

**`test_sold_today_qty_based.py`**: 시나리오 6(쿨다운 제거 후
실제 재매수)의 `_try_buy()` 호출 1곳을 동일한 방식으로 고정.

두 파일 다 `from utils.time_utils import KST_TZ`를 추가하고
`FIXED_MARKET_TIME = datetime(2026, 7, 28, 10, 0, 0, tzinfo=
KST_TZ)` 상수를 신설 — 각 파일이 검증하려는 건 차단 사유
(`SkipReason` 등)나 재매수 판정 로직이지 시간 게이트 자체가
아니므로, 시간 게이트만 장중으로 고정해 본래 검증 목적과 무관한
실행 시각 의존성을 제거.

**작업 중 발견한 편집 실수**: `str_replace`로 3번 시나리오에
patch를 추가하는 과정에서 실수로 다음 섹션 헤더 주석과 함께
중복 호출 코드가 남는 사고가 있었음 — 파일 전체를 다시 열어
`view`로 정확한 현재 상태를 확인한 뒤 정리해 복구.

**재현 검증**: 두 파일을 각각 실행해 5/5, 9/9 통과 확인. 이후
`run_regression_tests.py` 전체를 실행해 **정확히 30/30** 통과를
확인. 같은 날 다른 시각(수정 전 실패를 재현했던 시각과 비슷한
KST 16:31경, 장 마감 후)에 다시 실행해도 동일하게 30/30이 나옴을
재확인 — 시각 의존성이 실제로 완전히 제거됐음을 실행 시각을
바꿔가며 검증.

**전체 회귀**: `run_regression_tests.py` — **30개 파일 전부
통과, 종료코드 0**. 로그/데이터 디렉토리 오염 없음 재확인.

### 1B 종료 조건 현황

```
timestamp·개수 품질 검증: 완료
캐시·KST 신선도 검증: 완료
OHLC 데이터 품질 검증: 완료
분석 예외 fail-close: 완료
stale 상태 오염 차단: 완료
stale SELL 세분화(entry_watch 포함): 완료
14:50 KST Clock: 완료
설정값 검증: 완료
공식 회귀 30/30: 완료(이번 1B.12)
Windows 실제 API 로그 확인: 미완료 — 사용자 확인 필요
```

**⚠️ 여전히 필요한 것은 이제 마지막 하나뿐입니다.** Windows
실환경에서 `[MIN_BOOTSTRAP] outcome=SUCCESS`, `returned=60`,
`returned_oldest`/`returned_newest`, `returned_order=ASC` 로그를
직접 확인하는 것 — 이게 확인되면 1B를 종료하고 1C(세션 지표
shadow 구현)로 넘어갑니다.

---

## 1C: 세션 지표(SessionMetrics) shadow 구현 (2026-07-28)

**배경**: 민우님이 Windows 실환경에서 확보한 `[MIN_BOOTSTRAP]`
로그를 검토한 결과, `outcome=` 필드가 없는 등 1B.5 이전 버전의
로그(구버전, 최신 diff 미반영 추정)로 확인됨 — 로그가 보여주는
실제 이상 증상(`returned=0`인데 `raw_received=900`으로 정상 수신)
을 최신 코드로 동일 조건 재현한 결과 `outcome=SUCCESS, returned=
60, returned_order=ASC`로 정확히 안전하게 처리됨을 확인. 이 결과와
함께 GPT가 1B 코드 레벨 종료를 승인했고, 1C(세션 지표 shadow
구현)로 진행하기로 합의.

### 목표

v1.5 종료 시점 GPT 구조 재검토에서 확인된 1번 문제 — `Minute
Analysis.day_high`/`day_low`/`vwap`이 이름과 달리 실제로는 "최근
60분"(`minute_bar_count=60` 고정) 값이지 "당일 전체" 값이 아님
(1A 단계 fixture로 실제 데이터에서 확인한 바 있음) — 을 고치는 게
아니라, 먼저 "진짜 당일 값"을 별도로 계산해 기존 "60분 롤링" 값과
**나란히 관찰**하는 shadow 단계. `experimental.session_metrics_
mode`가 `"off"`(기본값)면 완전 비활성, `"shadow"`일 때만 계산·
로그만 하고 실제 매매 판정에는 절대 영향 없음. `"enforce"`(기존
값을 세션 값으로 실제 교체)는 이번 단계 범위 밖.

### 구현

**`domain/market_regime/session_metrics.py` 신규**: 순수 함수와
`SessionMetrics` dataclass.
- `merge_session_bar()`: `{cntr_tm: MinuteBar}` 딕셔너리에 새 봉
  하나를 병합 — 동일 timestamp면 교체(누적 아님), dict를 직접
  변형하지 않고 새 dict를 반환하는 순수 함수.
- `build_session_metrics()`: 세션 누적 봉으로부터 `session_vwap`
  (세션 전체)/`session_high`/`session_low`/`rolling_vwap_20`/
  `rolling_vwap_60`/`recent_high_30`/`session_bar_count`/
  `session_metrics_ready`(빈 세션이면 `False`)를 계산. 딕셔너리
  삽입 순서에 의존하지 않고 매번 `cntr_tm` 문자열 기준으로 정렬 —
  재시도로 과거 봉이 나중에 도착해도 항상 올바르게 계산.
- VWAP 계산 공식은 `MinuteAnalyzer.analyze()`의 기존 방식(typical
  price = `(high+low+close)/3 * volume` 가중평균)을 그대로 재사용
  — 계산 공식 자체는 이번 단계에서 바꾸지 않는다는 원칙("RSI/MACD/
  진입점수/손절폭/트레일링 파라미터 이번 라운드 변경 금지"와 동일
  취지)에 따름. 세션 지표든 롤링 지표든 같은 공식으로 비교해야
  "무엇이 달라지는지"가 창(window) 차이만으로 명확해짐.
- `format_session_metrics_log_line()`: 세션 값과 기존 60분 롤링
  값(legacy)을 나란히 보여주는 관찰 로그 포맷팅.

**`TradingService` 연결**:
- `self._session_bars_by_symbol: dict[str, dict]` 신규 — 종목별
  세션 전체 누적. `session_metrics_mode`가 `"off"`면 전혀 안 쌓임.
- `_get_minute_analysis()`의 `MinuteDataResult` **반환 직전**에
  `_update_session_metrics_shadow()` 호출 — 이 위치가 핵심 안전
  설계: `analysis`/`entry_safe`/`source`/`reason` 등이 전부 계산된
  뒤, 반환문 바로 앞에서 로그만 남기는 부수효과이므로 반환값을
  건드릴 방법이 물리적으로 없음. `try/except`로 감싸 fail-open —
  shadow 계산이 예외를 던져도 경고만 남기고 무시.
- `_update_session_metrics_shadow()`: `session_metrics_mode !=
  "shadow"`면 즉시 반환(off에서 완전 비활성), 아니면 세션 누적에
  병합·계산 후 60초 스로틀(기존 `[V_FAIL]`/`[MIN_STALE]` 로그와
  동일 cadence 원칙)로 `[SESSION_SHADOW]` 로그 출력.
- `reset_daily_loss_counts()`(날짜 변경 감지 시 호출되는 기존
  지점)에 `self._session_bars_by_symbol.clear()` 추가 — 전날
  세션 데이터가 새 거래일 계산에 섞이지 않도록.

### 검증

**핵심 안전 조건 — off와 shadow의 반환값이 완전히 동일함**:
시각을 고정한 뒤 `session_metrics_mode="off"`와 `"shadow"` 각각
으로 `_get_minute_analysis()`를 호출해 `MinuteDataResult`를
dataclass 전체 비교(`==`) — 완전히 동일함을 확인(`analysis`
내부의 33개 필드 포함 전부 일치). shadow 계산이 강제로 예외를
던지는 시나리오에서도 `entry_safe`/`analysis`가 정상 계산됨을
확인(fail-open).

**실제로 세션과 60분 롤링이 갈라지는 것을 재현 확인**: 1A fixture와
유사한 시나리오(전날 44개 + 오늘 봉이 시간에 따라 16→70→130개로
증가, API는 항상 최근 60개만 반환) 구성 — 시간이 지날수록 세션
누적 개수가 계속 증가(60→114→174)하고, 충분히 시간이 지나면
`rolling_vwap_60`(최근 60개, 기존 롤링과 유사한 개념)이 전날
데이터 없이 오늘 값으로 수렴하는데, `session_vwap`(세션 전체)은
전날 데이터의 영향이 남아 두 값이 실제로 벌어짐을 확인 — shadow
관찰 메커니즘 자체가 의도대로 작동함을 실증.

**⚠️ 이번 단계가 해결하지 않는 것 (명시적 한계)**: API 응답 자체가
이미 전일 봉을 포함해서 오는 문제(1A에서 확인한 근본 원인) 자체는
이번 1C shadow가 고치지 않습니다 — 세션 누적도 그 오염된 API
응답을 그대로 받아 누적하므로, 세션 첫 시작 시점에 전일 데이터가
한 번 섞여 들어가면 그 흔적이 `session_low`/`session_high`에
계속 남을 수 있습니다(테스트 9번에서 `session_low=49900`(전날
저가)이 계속 남는 것으로 실증). 이건 정확히 GPT 원래 8개 문제
목록의 1번 항목 자체를 고치는 것이 아니라, "지금 실제로 60분
롤링과 세션 전체가 이만큼 다르다"는 것을 정량적으로 드러내는
관찰 도구 — 다음 단계(2단계 DecisionEngine 추출 이후, 또는 별도
라운드)에서 이 관찰 데이터를 근거로 실제 교체(enforce) 여부를
결정.

**`test_session_metrics_shadow.py`(신규, 22건) 전부 통과**: 순수
함수 검증(병합/교체/정렬무관/빈세션방어, 1~5번), off 모드 완전
비활성(6번), off/shadow 반환값 완전동일(7번, 핵심), shadow
예외방어(8번), 세션-롤링 실제 괴리 재현(9번), 날짜변경 초기화
(10번), 로그 포맷 N/A 방어(11번).

**전체 회귀**: `run_regression_tests.py` — **31개 파일(기존 30 +
신규 1) 전부 통과**, 종료코드 0. 로그/데이터 디렉토리 오염 없음
확인.

**현재 설정**: `settings.yaml`의 `experimental.session_metrics_
mode`는 여전히 `"off"` — 이번 커밋 자체는 실거래 동작을 전혀
바꾸지 않음. `"shadow"`로 전환은 민우님이 직접 설정을 바꾸고
Windows 실환경에서 `[SESSION_SHADOW]` 로그를 관찰하고 싶을 때
수동으로 진행.

---

## 1C.2: 세션 필터링 누락 및 analyzer 중복 호출 수정 (2026-07-28)

**배경**: 1C 최초 구현에 대한 GPT 코드리뷰에서, "SessionMetrics"
라는 이름과 달리 실제로는 날짜/시간 필터링이 전혀 없어서 API가
반환한 60개(전일 봉 포함 가능)를 필터링 없이 그대로 세션에
병합하고 있었다는 지적을 받음 — 재현 확인: 전일 43개+오늘 17개
입력 시 `session_bar_count`가 17이 아니라 60, `session_low`도
전일 저가로 오염. 구 테스트 9번이 "전일 영향이 남는 것"을 PASS로
인정하고 있던 것도 잘못된 기준이었음.

### 발견·수정한 4가지 문제

**1. 날짜·시간 필터 부재 (가장 심각)**: `_passes_session_filter()`
신설 — 봉이 (a) KST 대상 거래일과 같은 날짜인지, (b) 정규장
09:00~15:30 안인지 확인. 통과 못 한 봉은 `filtered_other_date_
count`/`filtered_outside_market_count`로만 집계하고 세션에는
들어가지 않음. `REGULAR_MARKET_CLOSE`(15:30) 상수는 원래 `infra/
broker/minute_bar_diagnostics.py`(1B단계 진단 목적)에 있었는데,
domain 레이어(`session_metrics.py`)가 infra를 import하면 계층
방향이 역전되므로 공통 위치인 `utils/time_utils.py`로 옮기고
양쪽이 재사용하도록 정리.

**재현 검증**: 전일 43개+오늘 17개 입력 → `session_bar_count=17,
session_low=59900(오늘 저가), filtered_other_date_count=43` —
GPT가 명시한 기대값과 정확히 일치. 장전(08:30~08:34) 5개 봉 →
`filtered_outside_market_count=5` 확인.

**2. `session_metrics_ready` 의미 수정**: 기존 "봉이 하나라도
있으면 True"에서 "세션 히스토리를 장 시작부터 확보했는가"로 변경
— `readiness_reason`을 `"COMPLETE_FROM_OPEN"`(가장 오래된 세션
봉이 09:00~09:01 구간)/`"PARTIAL_SESSION"`(중간부터 시작)/
`"NO_SESSION_DATA"`(세션 봉 자체가 없음) 세 가지로 명시.

**재현 검증**: 09:00부터 시작한 세션 → `ready=True, COMPLETE_
FROM_OPEN`. 프로그램을 13시에 시작해 12:01~13:00만 가진 경우 →
`ready=False, PARTIAL_SESSION`(정확히 GPT가 제시한 시나리오와
일치).

**3. 종목별 `session_date` 자동 추적**: `SessionState`(dataclass,
`session_date` + `bars` 딕셔너리 + 필터링 카운터)를 신설해 종목별
로 보관. `merge_session_bars()`가 기존 상태의 `session_date`와
새로 계산 대상인 날짜가 다르면 **자동으로** 빈 세션부터 다시
시작 — `reset_daily_loss_counts()` 호출이 누락되어도 이 함수
레벨에서 전일 혼입을 방지.

**재현 검증**: `reset_daily_loss_counts()`를 호출하지 않고 다음날
봉을 바로 병합해도, 전일 17개가 섞이지 않고 새 세션(5개)만 남음을
확인.

**4. shadow 내부의 `MinuteAnalyzer.analyze()` 재호출 제거**: 기존
`_update_session_metrics_shadow()`가 `legacy_analysis`를 얻기
위해 `self._minute_analyzer.analyze()`를 다시 호출하고 있었음 —
`MinuteAnalyzer`가 `_last_v_fail_reasons`를 바꾸는 상태성 객체라,
이 재호출 자체가 "shadow는 상태를 안 바꾼다"는 원칙 위반(재현
확인: off는 `analyze()` 1회, shadow는 2회 호출). 이미 `_get_
minute_analysis()`에서 계산해둔 `analysis`를 그대로 인자로
전달받도록 시그니처 변경(`_update_session_metrics_shadow(symbol,
bars, analysis)`) — `legacy_vwap`/`day_high`/`day_low`는 전달받은
`analysis`에서 읽고, 함수 내부의 재분석 코드를 완전히 제거.

**재현 검증**: off/shadow 각각 `analyze()` 호출 횟수를 카운터로
직접 측정 — 수정 후 둘 다 정확히 1회. `MinuteAnalyzer._last_v_
fail_reasons`(analyzer 내부 상태)도 off/shadow에서 완전히 동일함을
확인.

### 5. 로그·SessionMetrics 필드 확장

`SessionMetrics`에 `session_date`/`earliest_timestamp`/`latest_
timestamp`/`rolling_20_count`/`rolling_60_count`/`filtered_
other_date_count`/`filtered_outside_market_count`/`readiness_
reason` 추가. `format_session_metrics_log_line()`도 이 필드들을
전부 출력하도록 확장.

### 6. `merge_session_bars` 배치 함수로 전환 (성능)

기존 `merge_session_bar()`를 봉 개수만큼(최대 60회) 반복 호출하며
매번 dict 전체를 복사하던 것을, `merge_session_bars()` 배치
함수로 바꿔 dict 복사를 세션 갱신당 1회로 줄임.

### 7. 구 테스트 9번(현재 12번) 기대값 반전

"전일 영향이 `session_vwap`에 남는 것"을 PASS로 인정하던 기존
기준을 GPT 지시대로 반전 — 이제 "전일 봉이 필터링되어 `session_
vwap`/`session_low`에 전혀 영향을 주지 않는 것"이 성공 기준.

### `test_session_metrics_shadow.py` 전면 재작성 (22→34건)

GPT가 지시한 7가지 항목을 전부 반영해 재작성: 날짜·시간 필터링
정확한 기대값 검증(1~2번), readiness 의미 수정 검증(3~5번),
`session_date` 자동 초기화 검증(6번), off/shadow `analyze()` 호출
횟수 동일성 검증(7~8번), `MinuteDataResult`와 analyzer 내부 상태
완전 동일성 검증(9번), shadow 예외 방어(10번), off 완전 비활성
(11번), 전일 오염 필터링 실제 확인(12번, 기대값 반전), 로그 필드
확장 확인(13번).

**전체 회귀**: `run_regression_tests.py` — **31개 파일 전부
통과**, 종료코드 0. 로그/데이터 디렉토리 오염 없음 확인.

**현재 설정**: `settings.yaml`의 `session_metrics_mode`는 여전히
`"off"` — 이번 커밋도 실거래 동작을 전혀 바꾸지 않음. 이 수정이
반영된 뒤에 `"shadow"`로 전환해 실제 관찰을 시작할 예정.

---

## 1C.3: session_metrics_mode를 shadow로 전환 (2026-07-28)

**배경**: 1C.2의 필터링·readiness·analyzer 중복호출 수정이 전부
검증된 후, GPT가 제시한 마지막 절차대로 `settings.yaml`의
`experimental.session_metrics_mode`를 `"off"`에서 `"shadow"`로
실제 전환.

### 전환 후 발견한 테스트 정합성 문제

`session_metrics_mode`를 `"shadow"`로 바꾸자 `test_experimental_
config.py`의 2번 검증이 실패함 — 이 테스트는 원래 "리팩터링 시작
전에는 모든 실험 플래그가 `off`"라는 것을 실제 `settings.yaml`
에서 검증하는 용도로 작성되어 있었는데, 이제 1C가 의도적으로
`shadow`로 전환되면서 그 전제 자체가 깨진 것 — **이건 회귀가
아니라, 테스트가 "1단계 착수 전" 시점의 스냅샷을 검증하도록
고정되어 있었던 것**.

**수정**: 2번 검증을 "전부 off"라는 고정 기준에서 "각 플래그가
해당 단계의 실제 진행 상황과 일치하는가"로 갱신 —
`session_metrics_mode`만 `"shadow"`(1C.2 완료, 관찰 중)를 기대
하고, 나머지 5개 플래그(`decision_engine_mode` 등, 아직 착수
전인 2~6단계)는 여전히 `"off"`를 기대. 6번 검증(experimental
섹션이 아예 없는 YAML의 기본값)은 그대로 유지 — 이건 `Experimental
Config` dataclass 자체의 기본값 검증이라 `settings.yaml`의 실제
값 변경과 무관하게 항상 "전부 off"가 맞아야 함(새 프로젝트를
처음 세팅할 때의 안전한 기본 상태를 보장하는 것과, 지금 이
프로젝트가 실제로 어느 단계까지 진행됐는지는 서로 다른 질문).

**검증**: 수정 후 `test_experimental_config.py` 7/7 통과.
`session_metrics_mode="shadow"`가 실제로 적용된 상태에서 전체
회귀(`run_regression_tests.py`) **31/31 통과** — shadow가 매매
판단에 전혀 영향을 주지 않는다는 걸 실제 설정 파일 레벨에서도
재확인.

### 현재 상태

`config/settings.yaml`:
```yaml
experimental:
  session_metrics_mode: "shadow"     # 1단계: 1C.2 검증 완료, shadow 관찰 중
  decision_engine_mode: "off"        # 2단계: 착수 전
  position_lifecycle_mode: "off"     # 3단계: 착수 전
  reward_risk_guard_mode: "off"      # 4단계: 착수 전
  candidate_ranking_mode: "off"      # 5단계: 착수 전
  trailing_breakeven_mode: "off"     # 6단계: 착수 전
```

이제 `[SESSION_SHADOW]` 로그가 실제로 남기 시작합니다 — Windows
실환경에서 며칠 관찰해, 실제 종목들의 세션 값(정규장 09:00~15:30
기준 진짜 당일 VWAP/고가/저가)과 기존 60분 롤링 값이 얼마나
차이 나는지 데이터를 쌓은 뒤, 그 결과를 근거로 다음 단계(실제
교체 여부 결정, 또는 2단계 DecisionEngine 추출 착수)를 판단.

**전체 회귀**: `run_regression_tests.py` — 31개 파일 전부 통과,
종료코드 0. 로그/데이터 디렉토리 오염 없음 확인.

---

## 1C.4: 세션 오염 방지 및 filtered unique count 핫픽스 (2026-07-28)

**배경**: 1C.3(shadow 실제 전환) 승인 직후, GPT 3차 코드리뷰로 실제
다일 관찰 시작 전에 반드시 고쳐야 할 두 가지 데이터 신뢰성 버그가
재현과 함께 지적됨.

### 1. 검증 실패 OHLC가 세션 저장소에 그대로 들어가던 문제 (재현 확인)

`_get_minute_analysis()`가 OHLC 구조 검증(1B Safety Closure)에
실패하면 `entry_safe=False, reason=INVALID_MINUTE_OHLC`로 안전하게
차단하지만, 정상 캐시가 없을 때 `bars=new_bars`(검증에 실패한 원본
그대로)를 유지한 채 `_update_session_metrics_shadow(symbol, bars,
analysis)`를 무조건 호출하고 있었음 — `session_metrics`의 필터는
날짜/시간만 검사해서 OHLC 오류는 걸러내지 못하니, 잘못된 60개가
그대로 세션에 들어갔음. 재현: `high=0, low=0`인 60개 응답 →
`_session_state_by_symbol[symbol].bars`에 41개(정규장 시간 필터만
통과)가 들어가고, 저장된 `high_price` 집합이 전부 `{0}`.

**수정**: `bars`(분석용 — 보유종목 판단이 끊기지 않도록 오염된
데이터라도 `MinuteAnalyzer.analyze()`에는 여전히 전달)와 `session_
ingest_bars`(세션 반영용 — 구조 검증을 실제로 통과한 데이터만)를
명확히 분리. `_get_minute_analysis()`의 각 코드 경로(빈 응답/구조
검증 실패/퇴행 응답/정상 응답/예외/캐시 재사용)마다 `session_
ingest_bars`를 명시적으로 결정 — 정상 응답(`source="LIVE"`)일
때만 `new_bars`(이번에 검증을 통과한 데이터)를 세션에 넘기고,
나머지 모든 실패 경로는 "기존(구조 검증을 통과했던) 캐시가 있으면
그걸 다시 반영, 없으면 세션에 아무것도 안 넣음"으로 통일. `_update_
session_metrics_shadow()` 호출부도 `bars` 대신 `session_ingest_
bars`를 전달하도록 변경.

`session_metrics.py` 모듈 자체에도 2차 방어선 추가 — `_passes_
ohlc_validity()` 신설(open/high/low/close > 0, low≤open≤high,
low≤close≤high, volume/acc_volume ≥ 0), `_passes_session_filter()`
가 날짜·시간 필터에 더해 이 OHLC 검증도 함께 확인하도록 통합.
어떤 경로로든 이상한 봉이 들어와도 이 모듈 레벨에서 최종적으로
걸러짐.

**재현 시나리오로 실제 수정 검증**: 캐시가 없는 상태에서 OHLC
오류 60개 응답 → 세션 저장소 자체가 생성 안 됨(`session_ingest_
bars=[]`). 기존 정상 세션(49개)이 있는 상태에서 같은 오염 응답이
들어와도 → 세션 봉이 정확히 그대로 유지(`state_after.bars ==
bars_before`), `high_price` 집합에 0이 전혀 섞이지 않음.

### 2. filtered 카운터가 overlapping API 창에서 중복 누적되던 문제 (재현 확인)

`filtered_other_date_count`/`filtered_outside_market_count`를
단순 정수로 누적하고 있어서, API 응답 창이 겹칠 때마다(예: 최근
60개 롤링에 같은 전일 10개가 매 폴링 계속 포함되는 정상적인
상황) 같은 봉을 매번 다시 세어 끝없이 불어남을 재현 확인 — 동일
전일10개+오늘10개를 같은 `SessionState`에 3회 병합 시 `filtered_
other`가 10→20→30으로 증가(실제 unique 전일 봉은 계속 10개인데도).

**수정**: `SessionState`의 필터링 카운터를 `set`(timestamp 기준)
으로 재설계 — `filtered_other_date_timestamps`/`filtered_outside_
market_timestamps`/`filtered_invalid_ohlc_timestamps`. `filtered_
*_count`는 이제 이 set의 길이(`len()`)로 계산되는 property라
같은 timestamp가 몇 번 다시 들어와도 unique 값은 정확. 동시에
`last_batch_filtered_*_count`(이번 병합 1회에서 새로 걸러진 개수,
중복 포함)도 별도 필드로 추가 — "이번 폴링에 몇 개가 새로
걸러졌는지"와 "지금까지 총 몇 개의 서로 다른 봉이 걸러졌는지"를
로그에서 둘 다 확인 가능. 로그 포맷을 GPT가 제시한 예시대로
`filtered_other_date_batch=43 filtered_other_date_unique=43` 형태로
변경.

**재현 시나리오로 실제 수정 검증**: 동일 전일10개+오늘10개를 3회
반복 병합 → `unique`는 계속 정확히 10 유지, `batch`는 매번 10을
그대로 보여줌. 한 봉씩 이동하는 overlapping window(전일 43개
고정, 오늘 봉이 1~17분으로 늘어나며 API가 항상 최근 60개만 반환)
→ `filtered_other_date_unique`가 끝까지 정확히 43으로 유지.

### 부수 발견: `REGULAR_MARKET_CLOSE` import 정리

`_passes_session_filter()`에 OHLC 검증을 통합하며 `_passes_ohlc_
validity()`를 별도 함수로 분리 — 기존 1B Safety Closure의 OHLC
검증 로직(`_evaluate_bar_freshness` 내부)과 검증 기준을 동일하게
맞춤(중복 코드지만 계층이 달라 공유 함수로 묶지 않음 — `domain/
service/trading_service.py`와 `domain/market_regime/session_
metrics.py`가 서로 다른 재사용 맥락이라 각자 유지하는 편이 결합도
관점에서 더 안전하다고 판단).

### readiness 문서 표현 완화

GPT 지시대로 `session_metrics_ready`/`readiness_reason`의 docstring
표현을 "끊김없이 완전한 세션"에서 "장 시작 구간 봉을 포함한 세션"
으로 완화 — 현재 구현은 `earliest_timestamp`가 09:00~09:01
구간인지만 확인하고, 중간 결측 구간(예: 09:00~09:10, 09:20~현재)
은 검증하지 않는다는 것을 명시.

### `test_session_metrics_shadow.py` 확장 (34→47건)

GPT가 명시한 4가지 필수 신규 테스트를 정확히 반영: invalid OHLC
응답이 세션 상태에 안 들어감(14번), invalid 응답이 기존 정상
세션을 오염시키지 않음(15번), 동일 오염 창 반복 병합 시 unique
count 불변(16번), 한 봉씩 이동하는 overlapping window에서 batch/
unique count 정확(17번). `session_metrics` 모듈 자체의 2차 OHLC
방어 확인(18번)도 추가. 로그 필드 검증(13번)을 새 필드명(`_batch`/
`_unique` 접미사)에 맞게 갱신.

**재검증 중 발견한 기존 테스트의 시간 의존성 결함**: 12번 테스트
(세션-60분 롤링 실제 괴리 재현)가 `today_open2`를 09:01로 고정한
채 `now_kst()`를 실제 테스트 실행 시각 그대로 두고 있어서, 실행
시각이 09:01에서 많이 벗어나면(예: 09:50경) 최신 봉의 age가
`minute_bar_max_age_seconds`(120초)를 초과해 신선도 검증 자체에서
막혀버리는 flaky 문제를 발견(재현: entry_safe=False, 세션에
아무것도 안 쌓임). `now_kst()`를 각 단계의 "그 시점"으로 명시적
고정하도록 수정 — 실행 시각과 무관하게 항상 동일한 결과가 나오도록
함(1B.6/1B.12절에서 이미 다룬 것과 같은 유형의 결함을 이번엔 1C
테스트에서 발견·직접 수정).

**전체 회귀**: `run_regression_tests.py` — 31개 파일 전부 통과,
종료코드 0. 로그/데이터 디렉토리 오염 없음 확인.

**현재 상태**: `settings.yaml`의 `session_metrics_mode`는 여전히
`"shadow"` — 이번 핫픽스로 세션 데이터의 신뢰성이 확보됐으므로,
이제 실제 다일 관찰(Windows 실환경에서 `[SESSION_SHADOW]` 로그
누적)을 시작해도 안전합니다.

---

## 1D: 테스트 인프라 유실 발견 및 kiwoom_broker.py 버전 복구 (2026-08-04)

**배경**: 1C.4 승인 이후 여러 세션이 지나며 확보된 프로젝트 zip에서
`test_run_once_integration.py`(프로젝트 루트본)와 `run_regression_
tests.py`가 실제로 유실된 상태임을 발견 — CHANGELOG 0.5단계에는
"`tests/` 안의 중복 사본만 `legacy_tests/`로 옮겼다"고 기록되어
있으나, 실제로는 루트 원본 자체도 함께 사라진 상태였음(경위 불명).
8개 이상의 최신 안전 테스트(`test_stale_minute_data_safety.py`
등)가 전자에서 `build_minimal_settings()`를 가져다 쓰고 있어,
이 파일 없이는 회귀 스위트 대부분이 아예 실행 자체가 불가능했음.

### 더 심각한 발견: kiwoom_broker.py가 1B.5 이전 버전으로 되돌아가 있었음

`test_run_once_integration.py`를 복구하고 전체 회귀를 처음 돌려본
결과, `test_minute_bar_diagnostics.py`가 `IndexError: tuple index
out of range`로 실패 — 진단 키가 5요소(`response_outcome` 포함)를
기대하는데 실제 코드는 4요소를 반환하고 있었음. `infra/broker/
kiwoom_broker.py`를 직접 열어 확인한 결과, `_maybe_log_minute_bar_
diagnostics`(1B.5에서 `_try_log_minute_bar_diagnostics`로 개명되고
`response_outcome` 파라미터가 추가되기 **이전** 이름)가 그대로
남아있었고, `get_minute_bars()`도 1B.5에서 고쳤던 정확히 그 버그
(빈 응답 진단이 항상 먼저 실행되어 정상 응답 진단이 키 선점으로
스킵되는 구조)를 그대로 갖고 있었음.

**이건 이전 세션(2026-08-04 이전)에서 민우님이 공유해주신 실제
운영 `app.log`가 `outcome=` 필드 없이 `returned=0`으로 찍혔던
것과 정확히 같은 증상** — 그때는 "구버전 로그일 것"이라고 추정만
했었는데, 이번에 실제 프로젝트 zip으로 그 추정이 사실이었음을
확인. 파일 내부 타임스탬프로도 `kiwoom_broker.py`만 7/27(1B.5
이전)이고 `domain/service/trading_service.py`(1C.4까지 반영, 7/29)
등 다른 핵심 파일은 최신이라는 게 명확히 드러남 — 즉 diff를
Windows에 적용하는 과정에서 이 파일 하나만 여러 차례에 걸쳐
누락됐던 것으로 추정.

**참고로 `minute_bar_diagnostics.py`(진단 데이터클래스·포맷팅
함수) 자체는 최신 상태였음** — `response_outcome` 필드가 이미
존재. 즉 딱 `kiwoom_broker.py`가 이 새 인터페이스를 호출하는
방식만 구버전으로 남아있던, 부분적 불일치였음.

**중요**: `domain/service/trading_service.py`는 `get_minute_bars()`
의 반환 타입(`list[MinuteBar]`)만 사용하고 내부 진단 로직과는
무관하므로, 이 버전 불일치가 실제 매매 판단(신규 진입 차단, stale
데이터 처리 등)에는 영향을 주지 않았음 — 영향 범위는 순수하게
"운영 모니터링 로그의 정확성"에 한정됨. 다만 이건 GPT가 여러
차례 코드리뷰로 재현·수정했던 버그가 실서버에서 계속 재발하고
있었다는 뜻이라, 로그를 근거로 한 향후 판단(예: 실제 raw 데이터
품질 확인)이 매번 왜곡된 정보를 보고 있었다는 문제.

### 수정

`kiwoom_broker.py`의 `get_minute_bars()`를 CHANGELOG 1B.5절 기록
그대로 재현해 복구:
- 빈 응답 진단(`response_outcome="EMPTY"`)과 정상 응답 진단
  (`response_outcome="SUCCESS"`)을 물리적으로 분리된 코드 경로
  에서만 각각 정확히 한 번씩 호출하도록 재구성.
- `_maybe_log_minute_bar_diagnostics` → `_try_log_minute_bar_
  diagnostics`로 개명, `response_outcome` 파라미터 추가.
- 진단 키를 `(symbol, base_date, tick_scope, count)` 4요소에서
  `(symbol, base_date, tick_scope, count, response_outcome)` 5요소
  로 확장 — EMPTY와 SUCCESS가 서로 다른 키를 가져 절대 서로를
  밀어내지 않음.
- `__init__`의 `_minute_diagnostic_keys` 타입 힌트도 5요소로 갱신.

**재현 시나리오로 실제 수정 검증**: raw 70개/count 60 정상 응답을
그대로 넣어 확인 — 수정 전이었다면 `returned=0, returned_order=
N/A`로 찍혔을 상황에서, 수정 후 `outcome=SUCCESS, returned=60,
returned_oldest`/`newest`가 실제 값으로 채워지고 `returned_order=
ASC`로 정확히 확인됨. 빈 응답 → 정상 응답 연속 호출 시나리오도
`EMPTY` 1회 + `SUCCESS` 1회로 정확히 분리됨을 재확인.

`test_minute_bar_diagnostics.py` 84/84 전부 통과 확인.

### test_run_once_integration.py 재작성

기존 파일을 되찾지 못해, 이 시점의 `config/settings.py`(1B.9~1C.4
반영, `MarketRegimeConfig`에 `minute_bar_min_count_for_entry`/
`minute_bar_max_age_seconds`/`minute_fetch_backoff_seconds` 등
신규 필드 다수 포함)의 모든 dataclass 필드를 하나씩 대조해 `build_
minimal_settings()`를 새로 작성. `run_once()` 통합 테스트 본체
(보유 종목 우선 처리 순서 검증)도 함께 복원. 이 헬퍼에 의존하던
6개 최신 안전 테스트(`test_stale_minute_data_safety.py` 등)가
전부 정상 실행됨을 확인.

`run_regression_tests.py`도 순수 실행기(테스트 로직 없이 `test_
*.py`를 찾아 subprocess로 순차 실행)라 그대로 재사용.

### 정식 실행 불가로 판단해 스킵 처리한 것

`test_legacy_fixture_structure.py`가 요구하는 `tests/fixtures/
legacy_20260721/end_of_capture_runtime_state.json`(1A 단계에서
실제로 캡처한 `data/state.json` 스냅샷 — README.md에 "개별 판단
시점 재현에는 못 쓰는 캡처 종료 시점 데이터"라고 명시된 별도
목적의 실데이터 파일, 같은 디렉토리의 `runtime_state.json`으로
대체 불가)이 실제로 존재하지 않음. 이 파일은 실제 캡처 데이터라
임의로 재생성하면 검증 자체가 무의미해지므로, 원본을 다시 확보
하기 전까지 `run_regression_tests.py`에서 명시적으로 스킵하도록
`SKIP_TEST_FILES` 상수와 안내 메시지 추가(왜 스킵하는지 실행 시
마다 출력).

### 이번 라운드에서 다루지 않은 것

시간 관계상 `MACD 골든 필수`/`눌림목 VWAP+2% 차단`(GPT의 최근
매매 데이터 분석에서 나온 개선안)은 이번에 착수하지 않음 — 먼저
테스트 인프라와 실제 코드 버전을 정확히 맞추는 게 선행되어야
그 다음 작업의 검증도 신뢰할 수 있다고 판단.

**전체 회귀**: `run_regression_tests.py` — 9개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
로그/데이터 디렉토리 오염 없음 확인.

**⚠️ 여전히 필요한 것**: `test_legacy_fixture_structure.py`의
`end_of_capture_runtime_state.json` 원본을 다시 확보하거나(1A
단계 작업 당시의 다른 백업이 있다면), 없으면 이 테스트를 완전히
폐기하고 README.md도 함께 정리할지 판단 필요. 또한 이번에 발견한
"파일별 버전 불일치"가 `kiwoom_broker.py` 외에 다른 파일에도
있을 수 있으므로, 다음 기회에 CHANGELOG의 각 단계별 수정 내용과
실제 파일 상태를 전수 대조하는 작업을 권장.

---

## 1E: MACD 상태 shadow 관측 필드 추가 (2026-08-04)

**배경**: 7/30~8/4 실제 매매 성과 분석에서 체결 10건 중 MACD 데드
3건이 전부 손실이었음을 `trades.csv`의 `entry_reason` 텍스트
파싱으로 확인했으나, 이건 실제 체결된 극소수 케이스에만 있는
정보 — 매수로 안 이어진 수만 건의 HOLD/SKIP 판단에는 MACD 상태가
`signal_log.csv`에 전혀 기록되지 않아(재현 확인: 원본 컬럼에
`macd`/`macd_signal` 자체가 없었음) "MACD 데드 요구 게이트를 넣으면
몇 건이 추가로 막혔을지"를 과거 데이터로 계산할 방법이 없었음.
GPT 코드리뷰 지시대로, 실제 게이트를 넣기 전에 먼저 관측 필드부터
쌓기로 함 — 신호 판단 로직은 전혀 바꾸지 않음.

### 발견: 완전 신규 게이트가 아니라 기존 게이트 확장이 정확한 방향

코드를 먼저 읽어본 결과(GPT 지시), `domain/strategy/breakout_
strategy.py`에 이미 `chasing_overheated`(당일 등락률 >= 3% AND
MACD 데드 → `min_score`를 3에서 5로 상향)라는 게이트가 존재했음.
이전 분석에서 "PR 조건이 VWAP+2% 기준"이라고 정리했던 건 부정확
— 실제로는 "고가 대비 -1%~-7% 눌림" 기준. MACD 데드 3건 중
005930(5점)/047040(6점)은 이미 5점 이상이라 기존 게이트를 통과할
자격이 있었고, 002990(4점)만 등락률이 3% 미만이라 게이트 자체가
발동 안 했던 것으로 추정 — 즉 "MACD 데드 완전 차단"을 새로
만들면 기존 게이트와 중복/상충되므로, 이 프로젝트의 기존 컨벤션
(`min_score` 상향 방식)을 따라 조건을 확장하는 게 안전.

### 구현

`infra/storage/logger.py`의 `SIGNAL_FIELDS`에 5개 필드 추가:
- `macd_golden`: `cond_macd_cross`(`macd > macd_signal`)와 정확히
  동일한 계산.
- `macd_dead`: `not macd_golden`.
- `macd_hist_dir`: `MarketPrice.indicator_macd_hist_direction`
  원시값 그대로.
- `chasing_overheated`: `breakout_strategy.py`의 실제 게이트
  조건(`당일등락 >= 3% AND MACD 데드`)과 한 글자도 다르지 않게
  재사용 — 다른 계산식을 쓰면 로그와 실제 동작이 미묘하게
  어긋나는 위험이 있어, 정확히 같은 식을 그대로 복사.
- `would_be_blocked_if_macd_dead_required`: "MACD 데드면 등락률과
  무관하게 무조건 5점 요구"였다면 이번 판단(`score < 5`)이
  막혔을지 여부 — shadow 전용, 실제 판단에는 미반영.

지표가 없는 경우(`macd is None`)나 이 필드가 애초에 의미가 없는
경우(MACD 골든일 때의 `would_be_blocked`)는 `False`로 단정하지
않고 빈 값으로 남김 — "관측 불가"와 "조건이 거짓"을 혼동하지
않도록.

`domain/service/trading_service.py`의 `_write_signal_log()`에
`market_price` 파라미터 추가(기본값 `None`, 하위 호환 유지),
`_process_symbol()`의 호출부에서 이미 스코프에 있던 `market_price`
를 그대로 전달. 신호 판단(`Signal` 생성)은 이 함수 호출 이전에
이미 끝나 있으므로, 이 로깅 추가가 판단 로직에 물리적으로
개입할 방법이 없음.

기존 `SignalCsvLogger._migrate_header_if_needed()`가 이미 "헤더에
없는 새 필드를 자동으로 추가"하도록 설계되어 있어(0.5단계에서
`atr_14` 등 추가 시 만든 로직), 별도 마이그레이션 코드 없이도
53MB 규모의 기존 실서버 `signal_log.csv`가 재시작 시 자동으로
새 헤더를 갖추도록 재확인(시뮬레이션으로 검증).

### 검증

- MACD 데드 + 당일등락 4% → `chasing_overheated=True`(실제 게이트
  조건과 정확히 일치).
- MACD 데드 + `score=4`(5점 미만) → `would_be_blocked=True`.
- MACD 데드 + `score=6`(5점 이상) → `would_be_blocked=False`(이미
  기존 게이트를 통과할 자격이 있었다는 뜻).
- MACD 골든 → `would_be_blocked`는 빈 값(관측 대상 아님).
- 지표 없음(`macd=None`) → 5개 필드 전부 빈 값(관측 불가를
  명확히 구분).
- `market_price` 생략(하위 호환) → 예외 없이 정상 기록, 5개
  필드 전부 빈 값.
- **핵심 안전 조건**: `_process_symbol()` 통합 흐름에서 `strategy.
  generate_signal()`이 반환한 `Signal`을 그대로 `patch`로 고정한
  뒤, `signal_log.csv`의 `skip_reason`이 그 원본 `reason`과 정확히
  일치함을 확인 — 관측 필드 계산 로직이 신호 자체를 조금도 바꾸지
  않음을 통합 레벨에서 재확인.
- 기존 대용량 CSV(구버전 헤더)에 대해서도 헤더 마이그레이션이
  정상 동작해 새 5개 필드가 추가됨을 시뮬레이션으로 확인.

`test_macd_shadow_observation.py`(신규, 19건) 전부 통과.

**전체 회귀**: `run_regression_tests.py` — 10개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
로그/데이터 디렉토리 오염 없음 확인.

**다음 단계**: 이 관측 필드가 실서버에서 며칠(GPT 권장 최소 3~5
거래일) 쌓이면, `chasing_overheated=True`인 경우와 `would_be_
blocked_if_macd_dead_required=True`인 경우가 실제로 몇 건인지,
그리고 그 판단들이 만약 매수로 이어졌다면 어떤 성과를 냈을지를
데이터로 계산할 수 있음. 그 결과를 보고 실제 게이트를 shadow로
넣을지, 그대로 관측만 계속할지 판단. 이번 라운드에서는 실거래
동작을 전혀 바꾸지 않음.

---

## 1E.2: MACD shadow 필드 재설계 — hard gate/min5 분리 (2026-08-04)

**배경**: 1E 1차 구현에 대한 GPT 코드리뷰에서 필드 의미 자체가
검증 대상과 다르다는 지적을 받음. 원래 검증하려던 가설은 "MACD가
Signal 이하이면 점수와 무관하게 완전 차단"(hard gate)이었는데,
1차 구현의 `would_be_blocked_if_macd_dead_required`는 실제로
`macd_dead and score < 5`(기존 `chasing_overheated`의 min-score-5
확장판)를 계산하고 있었음 — 재현 확인: "MACD 데드 + 6점" 케이스가
hard gate 기준에서는 `True`(6점이어도 완전 차단 대상)여야 하는데,
1차 필드는 6점이 이미 5점을 넘으므로 `False`로 나오고 있었음.

### 발견·수정한 4가지 문제

**1. hard gate와 min-score-5를 하나의 필드로 뭉뚱그림**: `would_
block_macd_dead_min_score5`(기존 게이트 확장판)와 `would_block_
macd_above_signal_required`(원래 검증 대상, 점수 무관 완전 차단)
두 필드로 분리. 재현 검증: dead+BUY+6점 → `hard=True, min5=False`
(1차 버전이었다면 여기서 `False`가 나왔을 정확히 그 케이스),
dead+BUY+4점 → `hard=True, min5=True`.

**2. HOLD 행에서도 "차단됐을 것"을 계산**: legacy 전략이 실제로
BUY를 반환하지 않은 판단(HOLD 등)을 "차단"이라고 부르는 건
counterfactual의 정의 자체가 안 맞음 — `legacy_buy_candidate`
(`signal.type == SignalType.BUY`) 조건을 추가해, 이 조건이 참일
때만 hard/min5를 계산하고 그 외엔 빈 값으로 남김. MACD 상태 관측
값(`macd_above_signal` 등)은 HOLD에도 계속 기록.

**3. 원시값(`macd`, `macd_signal`) 누락**: Boolean만 남기면 향후
임계값을 바꾸거나 계산 오류를 재검증할 방법이 없다는 지적에 따라
`macd`, `macd_signal` 원시 필드 추가.

**4. `latest_bar_timestamp` 없음**: 같은 1분봉에서 10~15초마다
반복 기록되는 `signal_log.csv`의 특성상, 정확한 dedup을 위해
필요 — `_process_symbol()`에서 `minute_result.latest_bar_
timestamp`를 안전하게 초기화(BEARISH/UNKNOWN 등 `minute_result`
자체가 정의 안 되는 경로에서도 `NameError` 없이 빈 값 처리)한 뒤
`_write_signal_log()`로 전달.

**5. `chasing_overheated`가 NEUTRAL에서도 계산됨**: 이 게이트는
`BreakoutStrategy`(BULLISH 전용)에만 실제로 존재하는데, NEUTRAL
등 다른 장세에서도 조건식만 계산해 "적용되지도 않는 게이트가
발동했다"는 거짓 신호를 낼 위험이 있었음 — `chasing_overheated_
applies`(`regime == MarketRegime.BULLISH`) 필드를 신설해 구분.
NEUTRAL이면 `applies=False`이고 `chasing_overheated` 자체는 빈 값.

**6. 이름 변경**: `macd_golden`/`macd_dead` → `macd_above_signal`
로 통일 — "골든크로스"(방금 교차가 일어났는지)가 아니라 "지금
`macd > macd_signal`인 상태"를 뜻하므로, 실제 이벤트처럼 들리는
이름을 정확한 이름으로 교체.

**7. 53MB급 CSV 마이그레이션이 원본을 직접 덮어쓰는 위험**:
`SignalCsvLogger._migrate_header_if_needed()`가 기존엔 전체 파일을
메모리에 읽어 `self.file_path`를 `"w"` 모드로 직접 재작성하고
있었음 — 재작성 도중 프로세스가 죽거나 디스크 문제가 생기면
원본이 훼손될 위험. 이제 (1) 재작성 전 `.bak`으로 원본 백업,
(2) 임시 파일(`.tmp`)에 한 행씩 스트리밍으로 재작성(메모리 부담
없음), (3) `os.replace()`로 원자적 교체(중간 상태 없이 교체 전
아니면 교체 후만 존재) — 세 단계로 재작성.

### `SIGNAL_FIELDS` 최종 구조 (10개 필드)

```text
macd, macd_signal, macd_above_signal, macd_hist_direction,
legacy_buy_candidate, latest_bar_timestamp,
chasing_overheated_applies, chasing_overheated,
would_block_macd_dead_min_score5, would_block_macd_above_signal_required
```

### 재검증 (직접 코드 실행으로 확인)

- dead+BUY+6점 → `hard=True, min5=False`(1차 버그였던 정확히 그
  케이스, 이제 정확).
- dead+BUY+4점 → `hard=True, min5=True`.
- dead+HOLD+4점 → `macd_above_signal=False`는 기록되지만 `hard`/
  `min5`는 빈 값.
- golden+BUY → `hard=False`.
- NEUTRAL → `chasing_overheated_applies=False`, `chasing_
  overheated=`빈 값.
- BULLISH+등락4%+MACD데드 → `chasing_overheated=True`(실제
  `breakout_strategy.py` 조건과 정확히 일치).
- BULLISH+등락2%(<3%)+MACD데드 → `chasing_overheated=False`
  (002990 4점 케이스처럼 실제 게이트가 발동 안 하는 상황 재현).
- 원시 `macd`/`macd_signal` 정확히 기록.
- BEARISH 경로에서도 `NameError` 없이 `latest_bar_timestamp` 빈
  값 처리.
- 통합 흐름(`_process_symbol()`)에서 `latest_bar_timestamp`가
  실제 최신 분봉과 일치.
- 마이그레이션 시 `.bak` 생성, 원본과 내용 일치, `.tmp` 정리됨,
  행 수 보존, 새 헤더 반영 — 전부 확인.

### `test_macd_shadow_observation.py` 전면 재작성 (19→39건)

GPT 2차 지시 내용을 전부 반영: `SIGNAL_FIELDS` 필드명 확인(1차
폐기 이름이 더 이상 없는지 포함), hard/min5 분리 4케이스, 원시값·
타임스탬프, `chasing_overheated_applies` 구분, BULLISH 실제조건
일치, 지표없음/하위호환, 신호판단 무영향 통합테스트, CSV 마이
그레이션 안전성 8케이스(백업/임시파일/행수보존/헤더반영) 포함.

**전체 회귀**: `run_regression_tests.py` — 10개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
로그/데이터 디렉토리 오염 없음 확인.

**현재 상태**: 이번 라운드도 실거래 동작을 전혀 바꾸지 않음 —
순수 관측 필드 재설계. 다음은 GPT가 제안한 1E.1(VWAP shadow:
`would_block_pr_only_vwap`, `would_block_c_or_pr_vwap`, `would_
block_pullback_condition_vwap`, `would_block_pr_or_pullback_
condition_vwap`)로, PR 조건이 실제로는 "고가 대비 -1%~-7% 눌림"
기준이지 VWAP 기준이 아니므로(1E 1차절 참고) `is_pulldown_
recovery`/`is_valid_pulldown`/조건검색식명 세 가지 범위를 각각
따로 관찰해야 함.

---

## 1E.3: minute_bar_saver.py 타임존 버그 발견 및 수정 (2026-08-04)

**배경**: 1E.2 전체 회귀 재확인 중 `test_minute_ohlc_quality_
safety.py`의 12번(정상 응답이 minute_bars CSV에 저장되는지 확인)
이 `FileNotFoundError`로 실패 — 이번 라운드의 코드 변경과 무관한
기존 버그를 우연히 발견.

**원인**: `infra/storage/minute_bar_saver.py`의 `MinuteBarSaver.
save()`가 저장 폴더의 날짜를 `datetime.now().date()`(시스템 로컬
시각)로 계산하고 있었음 — 이 프로젝트 전체가 KST 거래일 기준으로
동작하는데, 이 파일만 `now_kst()`를 쓰지 않고 있었음. Windows
실서버(시스템 로컬 시각이 이미 KST)에서는 우연히 문제가 드러나지
않았지만, 컨테이너처럼 시스템 로컬 시각이 UTC인 환경에서는 저장
폴더 날짜가 KST 기준 날짜와 최대 9시간까지 어긋날 수 있음(재현:
UTC 23:39=KST 08:39 시점에 테스트를 실행하면, 봉 자체는 KST
기준으로 만들어졌는데 저장 폴더는 UTC 날짜로 계산되어 하루 전
폴더에 저장됨 — 테스트가 `now_kst()` 기준 경로를 찾다가 실패).

**영향 범위**: 이 문제 자체는 실제 Windows 운영 환경에는 영향이
없었을 가능성이 높음(시스템 로컬 시각이 이미 KST이므로 `datetime.
now()`와 `now_kst()`가 사실상 같은 값을 반환). 다만 향후 이
프로젝트를 다른 시간대 서버(예: UTC 기준 클라우드 인스턴스)로
옮기게 되면 실제로 재현될 잠재적 버그였음 — 이번에 우연히
발견해 미리 차단.

**수정**: `datetime.now().date()` → `now_kst().date()`로 교체,
`utils.time_utils.now_kst` import 추가. 재현 시나리오(UTC 23:39
시점)로 실제 수정 검증 — 봉 생성과 경로 확인이 모두 KST 기준으로
일치해 정상 저장 확인.

`test_minute_ohlc_quality_safety.py` 26/26 재확인(수정 전 12번
실패, 수정 후 전부 통과).

**전체 회귀**: `run_regression_tests.py` — 10개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
로그/데이터 디렉토리 오염 없음 확인.

---

## 1E.4: chasing_overheated 관측범위 확장, 분봉 리플레이 날짜분리, timestamp KST 통일 (2026-08-04)

**배경**: 1E.2/1E.3 승인 직후, 데이터 수집을 시작하기 전 GPT 3차
코드리뷰로 4가지 보완사항이 지적됨 — 하나는 관측 의미 문제, 하나는
분봉 리플레이 데이터 오염 문제, 나머지 둘은 타임존 일관성.

### 1. `chasing_overheated`가 BUY 후보에만 계산되어 정작 실제 차단 사례를 못 봄 (재현 확인)

기존 `chasing_overheated_val`이 `legacy_buy_candidate_val`(신호가
BUY)일 때만 계산되고 있었는데, 실제 `BreakoutStrategy`의 `chasing_
overheated` 게이트가 진짜로 차단한 사례("등락4%+MACD데드+4점")는
전략 자체가 이미 `HOLD`를 반환함 — 그 HOLD 행에서는 `legacy_buy_
candidate_val=False`가 되어 `chasing_overheated`가 빈 값으로
남고, "기존 게이트가 실제로 몇 건을 막았는가"를 이 필드로 전혀
집계할 수 없었음(재현: 등락4%+MACD데드+4점 입력 → `legacy_buy_
candidate=False, chasing_overheated=""`).

**수정**: 조건 자체(`chasing_overheated_condition`)와 그 조건이
실제로 기존 게이트를 발동시켰는지(`would_block_existing_chasing_
gate`)를 `legacy_buy_candidate`와 무관하게(BUY든 HOLD든) 계산하도록
분리 — `chasing_overheated_applies`(BULLISH 여부)가 True일 때는
항상 계산됨. 신규 가상 게이트 두 필드(`would_block_macd_dead_
min_score5`, `would_block_macd_above_signal_required`)는 기존
정책대로 `legacy_buy_candidate=True`일 때만 계산 유지 — 이건
"새 규칙이 있었다면 이번 BUY 시도가 막혔을지"를 묻는 질문이라
BUY 후보 한정이 맞음, 반대로 `chasing_overheated_condition`은
"기존에 이미 있는 게이트가 지금 발동 중인지"를 묻는 질문이라
BUY/HOLD와 무관해야 함 — 두 질문의 성격이 다름.

재현 시나리오로 재검증: 등락4%+MACD데드+4점(HOLD) → `legacy_buy_
candidate=False`인 채로 `chasing_overheated_condition=True, would_
block_existing_chasing_gate=True`로 정확히 집계됨. 조건 자체가
거짓인 경우(MACD 골든 등)는 `would_block_existing_chasing_gate=
False`로 명시(빈 값 아님 — "발동 안 함"이 명확한 사실이므로).

### 2. `MinuteBarSaver`가 전일 봉을 오늘 리플레이 CSV에 섞어 저장 (재현 확인)

`save()`가 `target_date = now_kst().date()` 하나만 정해서 `bars`
전체를 같은 오늘 날짜 폴더에 저장하고 있었음 — 키움 최근 60봉
응답은 장 초반에 전일 봉과 오늘 봉이 섞여 오는 경우가 흔한데(예:
09:01에 전일 43개+오늘 17개), 그 60개가 전부 오늘 리플레이 CSV
파일 하나에 뒤섞여 저장되고 있었음(재현: 20260804 15:29 봉과
20260805 09:00 봉을 함께 넣으면 둘 다 `20260805/{symbol}.csv`
안에 섞여 들어감). 이건 1C단계 세션 지표에서 이미 재현·차단했던
것과 정확히 같은 유형의 오염이 리플레이용 원본 CSV에는 그대로
남아있던 것 — 오늘 폴더에 전일 봉이 섞이면 그 리플레이 파일로
계산하는 당일 고가·저가·VWAP과 A/B/C/V/PR 패턴의 장 초반 판정이
전부 왜곡될 수 있음.

**수정**: `save()`가 봉을 받으면 `cntr_tm`을 `parse_kst_bar_
timestamp()`로 파싱해 실제 날짜별로 그룹핑한 뒤, 각 날짜의
폴더에 나눠 저장하도록 재작성(`_save_for_date()` 헬퍼로 분리).
파싱 불가능한 timestamp(형식이 깨진 봉)는 조용히 건너뜀 — 저장
자체를 막지 않되 어느 날짜 폴더에도 잘못된 데이터를 넣지 않음.

재현 시나리오로 재검증: 전일43+오늘17 입력 → `20260804/{symbol}.
csv`에 정확히 43개, `20260805/{symbol}.csv`에 정확히 17개로
분리 저장, 각 파일에 다른 날짜 timestamp 0개 확인.

### 3. `signal_log.csv`의 `timestamp`가 시스템 로컬 시각 (재현 확인)

`_write_signal_log()`의 `timestamp` 필드가 `datetime.now().
isoformat()`(시스템 로컬 시각)을 쓰고 있어서, UTC 컨테이너 등
KST가 아닌 환경에서는 같은 행 안의 `latest_bar_timestamp`(KST
기준)와 최대 9시간까지 어긋날 수 있었음(재현: UTC 00:45 환경에서
`datetime.now()`는 00:45대인데 `now_kst()`는 09:45대). MACD
shadow가 신호를 시간대·분봉별로 묶어 분석할 예정이므로 함께 통일.

**수정**: `now_kst().replace(tzinfo=None).isoformat()`로 교체 —
기존 CSV의 timestamp 컬럼 포맷(타임존 표기 없는 ISO 문자열)과의
호환을 유지하면서 값 자체는 KST 벽시계 시각으로 통일.

재현 시나리오로 재검증: UTC 00:45(KST 09:45) 환경에서 기록된
`timestamp`가 정확히 KST 09시대로 확인.

### 4. (선택 개선, 함께 반영) CSV 마이그레이션에 flush+fsync 추가

`os.replace()` 자체는 이미 원자적이지만, 그 직전 `.tmp` 파일의
내용이 OS 캐시에만 있고 디스크에 아직 안 쓰인 상태에서 강한
장애(정전 등)가 나면 교체 후에도 불완전한 파일이 될 위험이
있어, `.tmp` 파일 close 전에 `flush()` + `os.fsync()`를 추가해
디스크 반영을 보장.

### 테스트 갱신

`test_macd_shadow_observation.py`를 GPT 3차 지시대로 갱신(39→55
건): `read_last_row()`를 `split(",")` 대신 `csv.DictReader`로
교체(향후 필드값에 쉼표가 섞여도 안전), `chasing_overheated` →
`chasing_overheated_condition`으로 필드명 갱신, HOLD 행에서도
기존 게이트가 정확히 집계되는지(17~18번), `MinuteBarSaver` 날짜
분리 저장(19~21번), `signal_log` timestamp KST 통일(22번) 신규
테스트 추가.

**전체 회귀**: `run_regression_tests.py` — 10개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
로그/데이터 디렉토리 오염 없음 확인.

**현재 상태**: 이번 라운드도 실거래 동작을 전혀 바꾸지 않음 —
순수 관측 필드 재설계 + 리플레이 데이터 저장 버그 수정. 이제
MACD shadow 관측을 실서버에서 시작해도 안전한 상태로 판단.
다음은 GPT가 제안한 VWAP shadow — PR 조건이 실제로는 "고가 대비
-1%~-7% 눌림" 기준이므로 `is_pulldown_recovery`/`is_valid_
pulldown`/조건검색식명 세 범위를 각각 따로 관찰.

---

## 1E.5: VWAP shadow 관측 구현 (2026-08-05)

**배경**: 매매 성과 분석에서 VWAP 대비 +2% 초과 진입 3건이 전부
손실 방향이었음을 확인했으나, PR(`is_pulldown_recovery`)과 C
(`is_valid_pulldown`)를 코드로 직접 읽어본 결과 둘 다 VWAP
거리(%)와 무관하다는 게 확인됨 — PR은 저점 우상향+거래량 팽창,
C는 "VWAP 위/아래"만 확인(몇 % 위인지는 무관). 즉 "VWAP +2%
초과 차단"은 기존 PR/C 계산식을 조정하는 게 아니라 완전히 새로운
진입 품질 게이트. GPT 코드리뷰 지시대로 다음 순서로 구현.

### 1. ConditionWatcher 복수 조건식 보존

기존 `symbol_to_condition`(단수형) property는 한 종목이 여러
조건식에 동시 편입돼도 마지막 seq 순회 결과 하나만 남겼음 —
`_symbols_by_seq`(dict) 순회 순서에 우연히 의존하는 결과라 실제로
"대표 조건식"을 의미 있게 고르는 게 아님. `symbol_to_conditions`
(복수형, `dict[str, tuple[str, ...]]`) property를 신규 추가해
편입된 모든 조건식 이름을 튜플로 보존.

재현 검증: 058610이 "자동매매_돌파형A"와 "자동매매_눌림목_PR"에
동시 편입된 경우, 기존 필드는 하나만 남기지만 신규 필드는 둘 다
보존함을 확인.

### 2. TradingService 스냅샷 교체 구조 (편출 잔존 방지)

`update_targets()`가 기존 `sym_to_cond`(단수형)는 그대로
`update()`로 누적하되, 신규 `sym_to_conditions`(복수형) 파라미터는
반드시 매번 전체 교체(`self._symbol_to_conditions = dict(...)`)
하도록 구현 — `update()`를 쓰면 이번 폴링에 편출된 종목의 과거
조건식 이름이 dict에 그대로 남아 "지금 이 종목이 눌림목 조건식에
편입돼 있는가"라는 질문에 거짓 True를 답하게 됨. `sym_to_
conditions`가 `None`으로 전달되면(조건검색 비활성 등) 기존
저장소를 비우지 않고 유지 — "결과가 없다"와 "전부 편출됐다"는
다른 신호이므로, 호출부가 명시적으로 빈 `dict`를 넘겨야 실제
전량 편출로 처리됨. `app/main.py`도 `watcher.symbol_to_conditions`
를 함께 전달하도록 갱신.

재현 검증: 1회차 눌림목 조건식 편입 → 2회차 완전 편출(빈 dict
전달) 시 과거 조건명이 `_symbol_to_conditions`에서 정확히 제거됨.

### 3. SessionMetrics 최신값 캐시

`_latest_session_metrics_by_symbol` 저장소 신설, `_update_
session_metrics_shadow()`가 `build_session_metrics()` 계산 직후
(60초 로그 스로틀 체크 이전)에 캐시하도록 수정 — 스로틀로 로그가
안 남는 폴링에서도 이 캐시는 항상 최신 상태 유지. `_write_signal_
log()`에서 세션 상태를 다시 계산하지 않고 이 캐시를 그대로 재사용.
일일 초기화(`reset_daily_loss_counts`) 시 함께 clear. 세션 값의
`session_date`가 오늘 거래일과 다르면(극단적 방어) 사용하지 않음.

### 4. 순수 평가 모듈 `domain/strategy/entry_quality_shadow.py`

`VwapShadowAssessment` dataclass와 `evaluate_vwap_shadow()` 순수
함수 신규 작성 — `Signal`이나 주문 결과를 절대 건드리지 않고
관측치만 계산해 반환. 핵심 설계:
- rolling VWAP(`MinuteAnalysis.vwap`, 최근 60분)과 session VWAP
  (1C단계 `SessionMetrics.session_vwap`, 당일 정규장 전체)을
  완전히 독립적으로 관측 — 두 기준의 성과를 직접 비교 가능하도록.
- `session_metrics_ready=False`(예: `PARTIAL_SESSION`)이면
  `session_vwap_distance_pct`는 관찰용으로 계산해 기록하되,
  `session_gate_eligible=False`가 되어 session 기반 `would_
  block_*`는 전부 `None`(빈 값)으로 남음 — 불완전한 세션 값을
  완전한 당일 VWAP처럼 오인해 판단에 쓰지 않도록.
- PR-only / C-or-PR / condition-source / PR-or-condition-source
  네 범위를 rolling·session 각각(총 8개 `would_block_*` 필드)
  독립적으로 관측 — 데이터가 쌓이기 전까지 어느 범위가 가장
  안정적인 개선인지 미리 확정하지 않음.
- 임계값은 정확히 `distance_pct > 2.0`(2.00%는 통과, 2.01%부터
  차단 후보).
- 모든 `would_block_*`는 `legacy_buy_candidate=True`(전략이
  실제로 BUY를 반환한 경우)일 때만 계산 — MACD shadow와 동일한
  원칙, HOLD였던 판단을 "차단"이라 부르지 않기 위함.

재현 검증: 임계값 경계(2.00%=False, 2.01%=True), `PARTIAL_
SESSION`일 때 거리는 기록/would_block은 빈 값, HOLD에서 상태값은
기록되지만 would_block 8개 전부 빈 값, 조건식명 없어도 PR=True면
정상 평가 — 모두 정확히 확인.

### 5. `experimental.entry_quality_guard_mode` 플래그

`off`/`shadow`만 허용, `"enforce"` 지정 시 `ExperimentalConfig.
__post_init__()`에서 명시적으로 `ValueError` — 이 단계는 shadow
관측까지만 구현했으므로, 설정 파일에 실수로 `"enforce"`가 들어가도
조용히 무시되는 대신("실제로는 shadow처럼 동작") 명확한 오류로
막음. `settings.yaml`에 `entry_quality_guard_mode: "off"`로 추가
(기본값, 아직 실서버에서 활성화 안 함).

### 6. 전용 로그 `logs/entry_quality_shadow.csv`

`infra/storage/logger.py`에 `ENTRY_QUALITY_SHADOW_FIELDS`와
`EntryQualityShadowLogger` 신규 추가 — `legacy_buy_candidate=True`
(BUY 후보)에만 기록, 중복 방지 키 `(symbol, latest_bar_timestamp,
detected_patterns, score)`로 같은 분봉·같은 판단의 반복 폴링이
중복 행을 만들지 않음. `signal_log.csv`(이미 53MB급)에는 원시
값·상태값(`is_pr`, `is_c`, `rolling_vwap_distance_pct` 등)만
추가해 기존 분석기가 HOLD/SKIP 포함 전체 판단에서 상태를 훑어볼
수 있도록 하고, 8개 `would_block_*` 상세는 전용 CSV로 분리.

재현 검증: 같은 (symbol, latest_bar_timestamp, patterns, score)
로 3회 연속 호출해도 `entry_quality_shadow.csv`는 1행만 기록,
`signal_log.csv`는 dedup 대상이 아니라 3행 그대로 기록됨(의도된
차이 — 전용 CSV만 후보 단위로 압축).

### 7. `TradingService` 통합

`_write_signal_log()` 끝부분(row가 이미 완성된 뒤)에서 `entry_
quality_guard_mode == "shadow"`일 때만 `evaluate_vwap_shadow()`
호출 — `off`(기본값)에서는 계산 자체가 스킵됨(빈 값). `__init__`
에 `entry_quality_shadow_logger` 선택적 파라미터 추가(기존
`entry_watch_shadow_logger` 등과 동일 패턴, `None`이면 storage
설정에서 자동 생성).

재현 검증(통합 흐름): `strategy.generate_signal()`을 `patch`로
고정한 뒤 `off`/`shadow` 두 모드로 `_process_symbol()`을 각각
실행 — `skip_reason`(최종 신호)과 `final_decision`이 완전히
동일함을 확인. VWAP shadow 계산이 실제 매매 판단에 조금도 개입하지
않음을 통합 레벨에서 재확인.

### 테스트

`test_vwap_shadow_observation.py`(신규, 34건): 범위 판정(PR-only/
C-or-PR/condition-source 분리), 복수 조건식 보존과 편출 스냅샷,
임계값 경계, session ready 여부에 따른 관찰/판단 분리, BUY 후보
한정, 조건명 누락 시에도 정상 평가, off/shadow 무영향(통합 흐름),
중복 방지 — GPT가 제시한 필수 테스트 항목 전부 반영. `test_
experimental_config.py`에도 `entry_quality_guard_mode` 검증
추가(9건, `enforce` 명시적 거부 케이스 포함).

**전체 회귀**: `run_regression_tests.py` — 11개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
기존 MACD shadow 테스트(55건) 그대로 유지. 로그/데이터 디렉토리
오염 없음 확인.

**현재 상태**: 이번 라운드도 실거래 동작을 전혀 바꾸지 않음 —
`entry_quality_guard_mode`가 `settings.yaml`에서 `"off"`로
시작하므로, 코드는 준비됐지만 아직 계산 자체가 실행되지 않는
상태. MACD shadow(1E단계)와 VWAP shadow(1E.5단계)를 동시에
`"shadow"`로 전환해 같은 기간에 함께 수집해야, 같은 시장·같은
종목·같은 BUY 후보에서 두 게이트를 직접 비교할 수 있음(GPT 지시).

---

## 1E.6: 조건검색식 출처 신뢰도 및 shadow 로그 개선 (2026-08-05)

**배경**: 1E.5 승인 직후, `entry_quality_guard_mode="shadow"`로
실전환하기 전 GPT 코드리뷰로 6가지 보완사항이 지적됨 — 그중 1번
(조건검색식 출처 부정확)은 shadow 데이터 자체를 오염시킬 수 있는
문제라 반드시 먼저 고쳐야 했음.

### 1. 실시간 REAL 이벤트의 조건검색식 출처 오류 (가장 중요, 재현 확인)

`ConditionWatcher._on_realtime()`에 `seq = next(iter(self.
_symbols_by_seq))`라는 코드가 있었는데, 주석은 "모든 단타 seq에
동시 귀속"이라고 되어 있었지만 실제로는 **첫 번째 seq 하나에만**
임의로 귀속시키고 있었음 — 키움 REAL 메시지 자체에 어느 조건식
에서 온 이벤트인지 정보가 없기 때문. 재현: seq1=돌파형A,
seq2=눌림목_PR 상태에서 058610이 실시간 편입되면, 실제 출처와
무관하게 항상 seq1 결과로만 기록됨. 이 오귀속 정보로 조건검색식
기반 VWAP shadow(`is_pullback_condition` 등)를 계산하면 통계
자체가 왜곡됨. 편출 처리도 같은 문제 — "임의로 고른 seq 하나
에서만 제거"해서 실제로 다른 조건식에 남아있는 종목을 잘못
제거할 위험이 있었음.

**수정**: GPT 권장 3번 방식(가장 보수적) 채택 — 실시간 이벤트는
"이 종목이 어떤 조건식엔가 소속돼 있다/아니다"라는 targets
갱신에만 쓰고, 어느 조건식인지는 확정하지 않음. `"__realtime_
unknown__"` 전용 버킷을 신설해 실시간 편입은 여기에 담아
`_all_symbols`(targets)에는 정확히 반영하되, `symbol_to_
condition(s)` 계산에서는 이 버킷을 제외 — 그 결과 이 종목은
조건검색식 이름 없이 targets에만 잡히고, 다음 `CNSRREQ` 재조회
때 정확한 seq로 확정됨. 편출은 "임의로 seq 하나 선택"이 아니라
그 종목이 들어있는 **모든 버킷에서 실제로 제거**하도록 재작성 —
"다른 조건식에 남아있는 종목을 잘못 제거"하던 위험을 근본적으로
없앰.

`_condition_source_reliable: dict[str, bool]` 저장소와 `symbol_
condition_source_reliable` property 신규 추가 — `_on_initial_
result()`(`CNSRREQ`, 메시지에 정확한 `seq` 포함)로 확정된 종목은
`True`, `_on_realtime()`(`REAL`, 출처 불명)으로만 알려진 종목은
`False`.

재현 검증: 실시간 편입 시 `symbol_to_conditions`에는 안 나타나고
`condition_source_reliable=False`, 하지만 `targets`에는 정확히
포함됨을 확인. `CNSRREQ` 초기조회 후에는 정확한 조건식명 +
`reliable=True`로 확정됨을 확인. 편출 시 두 조건식 모두에서 정확히
제거됨을 확인.

`domain/strategy/entry_quality_shadow.py`의 `evaluate_vwap_
shadow()`에 `condition_source_reliable` 파라미터 추가 —
`False`면 `is_pullback_condition`(및 그로부터 파생되는 4개
`would_block_*`)이 `None`, PR-only/C-or-PR 4개는 신뢰도와 무관
하게 항상 정상 계산(PR/C는 분봉 자체 분석값이라 조건식 출처
문제와 무관). `is_pr_or_pullback_condition`은 `is_pr=True`이면
`is_pullback_condition`이 `None`이어도 확정적으로 `True`가
되도록 3진 로직으로 정확히 구현(단순히 `bool(None)=False`로
암묵 변환하면 "조건식 소속이 아니다"로 잘못 단정하게 되는 문제를
재현으로 확인하고 수정) — `would_block_pr_or_pullback_condition_
*`도 이 3진 로직을 정확히 따라가도록 게이트 조건을 `is_
pullback_condition`과 `is_pr_or_pullback_condition` 각각
독립적으로 확인.

### 2. entry_quality_shadow.csv 중복 방지가 게이트 상태 변화를 삭제 (재현 확인)

기존 중복 방지 키가 `(symbol, latest_bar_timestamp, detected_
patterns, score)`뿐이라, 같은 1분봉 안에서 현재가가 움직여 게이트
상태가 바뀌어도(예: rolling 거리 1.99%→2.01%로 임계값을 넘어감)
두 번째 관측이 조용히 버려지고 있었음(재현: 첫 관측 block=False로
기록 성공, 두 번째 관측 block=True인데 같은 키라 `append_if_new`
가 `False`를 반환해 기록 안 됨 — 최종 CSV에는 첫 상태만 남음).

**수정**: 키에 `assessment_signature`(MACD 2개 + VWAP rolling/
session 8개 + `final_decision` + `order_block_reason`, 총 12개
필드) 추가 — 같은 분봉이라도 이 서명이 바뀌면 새 행으로 기록.
`_entry_quality_shadow_key()` 헬퍼로 계산을 통일하고, 모든 값을
`str()`로 정규화(재현 확인: Python `bool` 타입과 CSV에서 복원한
문자열 `"True"`가 그대로 섞이면 재시작 후 중복 감지가 실패하는
문제가 있어서 타입을 통일).

재현 검증: 1.99%→2.01% 상태 변화 시 정확히 2행 기록, 동일 상태
3회 반복은 1행 유지, `final_decision`이 `BLOCKED`→`BUY`로 바뀌면
새 행 기록.

### 3. entry_quality_shadow.csv 필드 추가 (실제 진입 기준값)

`current_price`, `legacy_reason`, `final_decision`, `order_
block_reason`, `actual_order_submitted`(`final_decision=="BUY"`),
`condition_source_reliable` 신규 추가 — 기존엔 `legacy_buy_
candidate=True`라도 실제로 주문됐는지, `DAILY_ENTRY_LIMIT`/
`AFTER_1450`/`RISK_LIMIT` 등 기존 규칙으로 이미 차단된 후보인지
전용 로그만으로는 구분할 수 없었고, `current_price`가 없어
5·10·20분 후 수익률 계산도 다른 로그와 복잡하게 조인하거나
VWAP 거리에서 역산해야 했음.

### 4. 재시작 시 중복 방지 복원

`EntryQualityShadowLogger.__init__()`이 기존 파일이 있으면(장중
재시작) 그 안의 모든 행에서 키를 읽어 `_seen_keys`를 복원하도록
수정 — 전용 파일은 legacy BUY 후보만 담아 크기가 작으므로 전체
재읽기 부담이 크지 않음. 복원 실패(파일 손상 등)는 경고만 남기고
빈 상태로 계속 진행(fail-open).

재현 검증: 재시작 시뮬레이션(같은 파일 경로로 새 로거 인스턴스
생성) → 기존 1개 키가 정확히 복원되고, 동일 판단 재시도 시
중복으로 정확히 거부됨.

### 5. 대표 condition_name 모순 해소

기존엔 `_symbol_to_condition`(단수형)을 `update()`로 별도 누적해서,
`_symbol_to_conditions`(복수형)는 스냅샷 교체로 고쳤는데도 단수형
대표값에는 편출된 종목의 과거 조건식명이 남아 `condition_name`
(옛값)과 `condition_names`(빈 문자열)가 서로 모순되는 로그 행이
생길 수 있었음. `_symbol_to_condition` 저장소 자체를 폐기하고,
`_representative_condition_name()` 메서드가 항상 `_symbol_to_
conditions`(같은 시점의 복수형 스냅샷)에서 정렬 후 첫 번째 값을
결정적으로 파생시키도록 변경 — 두 필드가 항상 같은 시점의 같은
데이터에서 나오므로 모순이 구조적으로 불가능해짐. `TradingService`
내 3개 참조 지점 전부 교체.

재현 검증: 복수 조건식 편입 시 대표값이 실제 스냅샷 안의 값 중
하나로 정확히 나옴, 편출 후에는 대표값도 정확히 빈 문자열(과거
값 잔존 없음).

### 6. off 모드 문서 불일치 정정

`EntryQualityShadowLogger` 생성자가 `entry_quality_guard_mode`와
무관하게 항상 헤더 파일을 즉시 생성하는 것 자체는 기존 다른
shadow 로거들과 동일한 패턴 — 코드 동작은 문제 없었으나 docstring
표현을 "`off`에서는 이 파일에 행이 추가되지 않을 뿐, 빈 헤더만
있는 파일 자체는 생성됨"으로 명확히 정정.

### 테스트

`test_vwap_shadow_observation.py`를 34→58건으로 확장 — 기존
`evaluate_vwap_shadow()` 호출부 10곳에 `condition_source_
reliable=True`(기존 테스트 의미 유지) 추가, GPT가 제시한 9가지
필수 신규 테스트(REAL 이벤트 신뢰도, CNSRREQ 확정, unreliable
시 조건-소스 `would_block=None`, PR/C는 신뢰도 무관 정상 계산,
1.99%→2.01% 상태변화 2행, 동일상태 반복 1행, `BLOCKED`→`BUY`
변화 새행, 실제 진입 기준값 기록, 재시작 후 중복방지, 대표
`condition_name` 일치) 전부 반영.

**전체 회귀**: `run_regression_tests.py` — 11개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
MACD shadow(55건) 그대로 유지, VWAP shadow 58건. 로그/데이터
디렉토리 오염 없음 확인.

**현재 상태**: 이번 라운드도 실거래 동작을 전혀 바꾸지 않음.
`entry_quality_guard_mode`는 여전히 `settings.yaml`에서 `"off"`.
이제 조건검색식 출처 문제가 해결됐으므로, `"shadow"`로 실전환해
MACD/VWAP 데이터를 동시에 수집해도 안전한 상태로 판단.

---

## 1E.7: shadow 실전환 전 최종 무결성 수정 (2026-08-05)

**배경**: 1E.6 승인 직후, `entry_quality_guard_mode="shadow"` 실전환
직전 GPT 3차 코드리뷰로 P0 2건 + P1/P2 각 1건이 지적됨. 모두
재현부터 확인 후 수정.

### P0-1: EntryQualityShadowLogger 헤더 마이그레이션 누락 (재현 확인)

1E.5 시절 헤더는 32열, 1E.6에서 6개 필드(`condition_source_
reliable` 등)가 추가돼 38열이 됐는데, `EntryQualityShadowLogger`
가 파일 존재 여부만 확인하고 헤더 스키마는 전혀 비교하지 않았음
— 이 로거는 `entry_quality_guard_mode="off"`일 때도 빈 헤더 파일을
생성하므로, 1E.5 코드를 한 번이라도 실행한 실서버에는 구형 헤더
파일이 있을 수 있었음. 재현: 구형 32열 헤더에 신형 38열 로거로
행을 추가하면, `csv.DictReader`로 다시 읽을 때 초과 6개 값이
`row[None]`으로 밀려나고 `final_decision`이 `None`으로 파싱됨.

**수정**: `SignalCsvLogger`의 마이그레이션 로직(백업 → 임시파일
스트리밍 → flush+fsync → `os.replace`)을 `_migrate_csv_header_
if_needed()` 범용 함수로 추출해 두 로거가 공유하도록 재작성.
`EntryQualityShadowLogger.__init__()`이 기존 파일이 있으면 키
복원 *전에* 반드시 이 함수를 먼저 호출.

재현 시나리오로 재검증: 마이그레이션 후 기존 행 보존, 신규 필드
정상 파싱, `.bak` 백업 생성 확인.

### P0-2: ConditionWatcher 신뢰도 로직 4가지 문제 (전면 재설계, 재현 확인)

기존 `_symbols_by_seq` + 누적 `_condition_source_reliable` dict
구조를 폐기하고, `_confirmed_symbols_by_seq`(CNSRREQ로 확정된
것만) + `_realtime_unresolved`(REAL로만 알려진 미확정 종목) 두
상태로 완전히 분리 — `reliable` 여부는 항상 이 두 상태에서 호출
시점에 재계산(`symbol_condition_source_reliable` property).

- **문제A** (재현 확인): 이미 CNSRREQ로 `reliable=True`가 확정된
  종목에 REAL I가 와도 `"symbol not in self._all_symbols"` 조건
  때문에 아무 처리를 안 해 `reliable=True`가 그대로 남았음 — 그
  REAL I가 실제로 다른(아직 모르는) 조건식에서의 신규 편입일 수
  있는데도. 수정: 이미 알려진 종목이어도 REAL I가 오면 무조건
  `_realtime_unresolved`에 추가해 즉시 `reliable=False`로 하향.
- **문제B**: REAL D가 그 종목이 소속된 모든 seq 버킷에서 한꺼번에
  제거하는 기존 동작 자체는 유지하되, 이게 "정확한 조건식별
  편출"이 아니라 "출처불명 이벤트는 안전을 위해 전체 제거하는
  보수적 정책"임을 코드 주석과 모듈 docstring에 명확히 정정
  (신규 진입 후보를 못 보는 게, 편출됐어야 할 종목을 계속 신호
  판단 대상으로 남기는 것보다 낫다는 원칙).
- **문제C**: 주기적 CNSRREQ 재조회 자체는 이번에도 구현하지
  않음 — REAL-touched 종목은 계속 condition-source 분석에서
  제외되는 게 맞다는 GPT 판단을 그대로 따름.
- **문제D** (재현 확인): `_on_initial_result()`가 새 결과에서
  사라진 종목의 stale reliability나 unresolved 상태를 정리하지
  않았음. 수정: 재조회 확정 결과로 `_realtime_unresolved`에서
  해당 종목들을 명시적으로 제거(`self._realtime_unresolved -=
  new_symbol_set`) — 재조회 결과에 없는 종목은 이 seq 버킷에서만
  빠지고 다른 seq·unresolved 상태는 그대로 유지("이 조건식에는
  없다"가 "완전히 알 수 없다"는 뜻은 아니므로).

재현 시나리오로 재검증: (A) 이미 known/reliable=True 종목에 REAL
I → reliable=False로 하향, targets는 유지. (D) unresolved 종목이
재조회로 확정되면 → reliable=True + unresolved에서 제거. (D-2)
재조회 결과에서 완전히 사라진 종목 → confirmed에서 제거되어
`symbol_condition_source_reliable`에도 안 나타남, targets에서도
제거.

부수 발견: `symbol_condition_source_reliable`의 docstring이 "계산식
자체로 없으면 False가 보장된다"고 썼는데 실제로는 confirmed_union
에 없는 종목은 딕셔너리에 키 자체가 없어 `.get(symbol)`이 `None`을
반환하는 것을 테스트 작성 중 재현 확인 — docstring을 실제 동작
("호출부는 반드시 `.get(symbol, False)`로 조회해야 함")에 맞게
정정. 실제 호출부(`TradingService._write_signal_log()`)는 이미
정확히 `.get(symbol, False)`로 처리하고 있어 프로덕션 동작에는
영향 없었음.

### P1: 주문 결과 필드 분리 (재현 확인)

기존 `actual_order_submitted = final_decision=="BUY"` 하나로는
"기존 리스크/제한 규칙을 통과해 실제로 `place_order()`를 호출
했는지"와 "브로커가 그 주문을 실제로 접수했는지"를 구분할 수
없었음 — `_try_buy()`가 `place_order()`를 호출한 뒤 `result.
accepted=False`(브로커 거부)여도 명시적 차단 사유를 반환하지
않아(암묵적 `None` 반환) `final_decision`은 여전히 `"BUY"`로
남는 것을 재현 확인.

**수정**: `_try_buy()`의 기존 반환 시그니처(차단 사유 문자열
또는 빈 문자열)는 `test_order_block_reason.py` 등 기존 회귀가
직접 비교하므로 그대로 유지. 대신 `_last_order_attempt_by_symbol`
저장소를 신설해 `place_order()` 호출 직후의 `OrderResult`를 별도
기록 — `_try_buy()` 호출 직전에 이전 폴링의 낡은 기록을 `pop()`
으로 먼저 지워서, "이번 폴링에서 새로 생긴 값이 있다"가 곧 "이번
폴링에서 실제로 `place_order()`가 호출됐다"를 정확히 의미하도록
설계. `ENTRY_QUALITY_SHADOW_FIELDS`의 `actual_order_submitted`를
`order_attempted`/`order_accepted`/`order_id`/`order_message`
4개로 분리.

재현 시나리오로 재검증: 브로커가 거부하도록 패치한 뒤 `_try_buy()`
호출 → `block=None`(리스크 게이트는 통과해 `place_order()`까지
호출됨) + `order_attempt.accepted=False`(브로커 거부) 정확히
분리 확인. 통합 흐름(`_write_signal_log()`)까지 재확인: `final_
decision="BUY"`인데 `order_accepted="False"`로 정확히 기록됨.

### P2: append_if_new 기록 순서 (이전 라운드에서 이미 반영 확인)

`_seen_keys.add(key)`를 `writer.writerow()` *이후*로 옮기는 수정
자체는 1E.6에서 이미 반영돼 있었음 — 이번 라운드에서 재확인만
수행. 테스트로 `open()`이 예외를 던지도록 강제한 뒤, 쓰기 실패
시 키가 소비되지 않아 재시도가 정상적으로 가능함을 직접 검증.

### 테스트

`test_vwap_shadow_observation.py`를 58→79건으로 확장 — GPT가
제시한 필수 테스트(28: 구형 헤더 마이그레이션, 29: known 종목
REAL I 시 하향, 30: unresolved 재조회 확정, 31: stale reliability
정리, 32: 브로커 거부 시 필드 분리, 33: 리스크게이트 조기차단
시 order_attempted, 34: 쓰기 실패 후 재시도 가능) 전부 반영.
기존 5/18번 테스트가 1E.6 이전 세대 필드명(`_symbols_by_seq`,
`_condition_source_reliable`)을 그대로 쓰고 있던 것도 이번에
발견해 신규 구조(`_confirmed_symbols_by_seq`, `_realtime_
unresolved`)로 갱신, 24번 테스트의 폐기된 필드명(`actual_order_
submitted`) 참조도 정리.

**전체 회귀**: `run_regression_tests.py` — 11개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
MACD shadow(55건), experimental config(9건) 그대로 유지. 로그/
데이터 디렉토리 오염 없음 확인.

**현재 상태**: 이번 라운드도 실거래 동작을 전혀 바꾸지 않음.
`entry_quality_guard_mode`는 여전히 `settings.yaml`에서 `"off"`.
P0 2건이 모두 해결됐으므로, 이제 `settings.yaml`만 별도 커밋으로
`"shadow"` 전환하면 MACD/VWAP shadow를 동시에 실서버에서 안전하게
수집 시작할 수 있는 상태.

---

## 1E.8: app/main.py의 ConditionWatcher 옛 필드명 참조 긴급 수정 (2026-08-06)

**배경**: 민우님이 실서버(08:45 장전)에서 프로그램을 실행하자마자
`WebSocket 연결 끊김: 'ConditionWatcher' object has no attribute
'_symbols_by_seq' — 5.0초 후 재연결`이 반복되는 것을 실제 로그로
보고 — 재연결이 계속 실패하며 루프에 빠지는 심각한 회귀.

**원인**: 1E.7에서 `ConditionWatcher`의 내부 필드명을 `_symbols_
by_seq` → `_confirmed_symbols_by_seq`로 바꾸면서(P0-2, confirmed/
unresolved 상태 분리), `condition_watcher.py` 자체와 이번 라운드에
새로 작성한 테스트는 전부 새 이름으로 정확히 갱신했지만, **`app/
main.py`의 `on_symbols_changed()` 콜백 안에서 이 필드를 직접
참조하던 3곳을 놓쳤음** — 단타/스윙 조건식 구분 필터링(day_
symbols 추림)과 `[COND_STATUS]` 로그 조합, 스윙 종목 파일 저장
로직. `ConditionWatcher.__new__()`로 인스턴스를 직접 만들어
테스트했던 `test_vwap_shadow_observation.py`나 단위 테스트로는
이 문제를 못 잡음 — `app/main.py`의 이 콜백 함수는 어떤 자동
테스트도 실행하지 않는 부분이었음(실제 웹소켓 콜백 배선 자체가
회귀 스위트의 사각지대였음).

**수정**: `app/main.py`의 `watcher._symbols_by_seq` 3곳을 전부
`watcher._confirmed_symbols_by_seq`로 치환 — 세 곳 모두 "특정
seq에 속한 종목만 추림"이라는 동일한 목적이라 단순 치환으로
의미가 그대로 보존됨.

재현 검증: 수정된 로직을 직접 재현해 `_confirmed_symbols_by_seq`
+ `_realtime_unresolved`(출처 불명 종목)가 섞인 상태에서 콜백
로직을 실행 — 예외 없이 정상 동작, `seq_info` 로그도 정확히
조합됨.

**부수 발견(별도 확인 필요, 이번 라운드에서는 미해결)**: `day_
symbols` 계산이 `_confirmed_symbols_by_seq`만 순회하므로, 출처
미확정 종목(`_realtime_unresolved`에만 있는 것)은 이 필터링에서
빠짐 — `_all_symbols`(targets)에는 포함되지만 `day_symbols`(→
`combined` → `trading_service.update_targets()`로 전달되는 최종
목록)에는 안 들어감. 다만 이건 1E.7이 새로 만든 회귀가 아니라
`__realtime_unknown__`(1E.6 시절 특수 버킷) 때부터 있던 동작으로
보임 — `day_seqs_set`(숫자 seq 문자열들)에 `__realtime_unknown__`
이 애초에 매칭될 수 없었으므로. 실제로 의도된 설계인지(출처
미확정 종목은 정식 판단 대상에서 빼는 게 맞는 정책인지) 아니면
발견되지 않은 별개의 결함인지는 확인이 더 필요함 — 다음 라운드
과제로 남김.

**전체 회귀**: `run_regression_tests.py` — 11개 파일 전부 통과,
1개(`test_legacy_fixture_structure.py`) 명시적 스킵, 종료코드 0.
이 회귀 스위트 자체는 `app/main.py`의 콜백을 실행하지 않으므로
이번 버그를 애초에 잡을 수 없었다는 한계를 확인 — 향후 `app/
main.py`의 `on_symbols_changed` 콜백을 직접 실행하는 통합 테스트
추가를 고려할 필요가 있음(이번 라운드에서는 긴급 수정만 반영).

---

## 1E.9: 실시간 편입 종목 targets 누락 P0 수정 + CNSRREQ 900003 회복 (2026-08-06)

**배경**: 1E.8 적용 후 실서버 로그를 재점검하는 과정에서, 1E.8에서
"의도된 설계인지 별개 결함인지 확인 필요"로 남겨뒀던 `day_symbols`
문제가 **실거래를 사실상 마비시키는 P0 결함**임이 실서버 로그로
확정됨.

### P0-1: 장중 조건검색 편입 종목이 targets에서 전량 누락 (재현 확인)

**증상 (실서버 8/6 로그)**:
```
09:10:10 [COND] [출처불명] 편입: 215790 (실시간 이벤트, ...)
09:10:10 [COND_STATUS] seq1=0 | seq2=1 | seq3=0 | excluded=0차단 |
         final=5종목: ['010170','006260','005930','080220','069540']
```
방금 편입된 215790이 최종 감시 목록에 없음. `_notify()`는
`sorted(self._all_symbols)`(= confirmed ∪ unresolved)로 콜백을
호출했지만, `on_symbols_changed()`가 **인자 `symbols`를 사용하지
않고** `_confirmed_symbols_by_seq`만 다시 순회해서 재계산하므로
출처 미확정 종목이 통째로 증발함.

**왜 치명적인가 (로그 실측)**:
- `CNSRREQ`는 `_on_login()`에서 **연결당 1회만** 발송되며 주기적
  재조회 코드가 없음 → 확정 버킷은 장전 스냅샷에서 갱신 안 됨.
- 장전 초기 조회 결과는 실측상 항상 0종목:
  `08:44:36 [COND] [자동매매_눌림목_PR] 초기 결과: 0개 종목`
  (seq1/2/3 전부, 8/5·8/6 동일).
- 즉 **장중 편입은 100% REAL 실시간 이벤트로만 들어옴.**
- 8/5(구 코드)에는 실시간 편입 3,318건 / 편출 3,203건이 발생하며
  seq 버킷이 `0/0/0`(08:44) → `52/3/23`(15:30)까지 성장했음.
- 신규 코드에서는 이 3,318건이 전부 `_realtime_unresolved`로만
  가므로, 확정 버킷은 하루 종일 비어 있고 **감시 대상이 수동
  targets 4종목뿐**이 됨. 8/6 로그가 정확히 그 상태(`final=4~5종목`).

**1E.7이 만든 회귀인가**: 실질적으로 그렇다. 1E.8 CHANGELOG는
`__realtime_unknown__`(1E.6) 시절부터 있던 동작으로 추정했고 그
추정 자체는 맞지만, 8/5까지 실서버에 떠 있던 코드는 실시간 편입을
seq 버킷에 직접 넣어 정상 동작했음(`[자동매매_눌림목_PR] 편입:
419050` 형태의 로그가 그 증거). 조건검색 자동매매가 실제로 죽은
것은 1E.6~1E.7 계열 변경이 실서버에 올라간 8/6이 처음.

**수정**: 출처 미확정 종목도 단타 감시 대상에 포함.
`symbol_condition_source_reliable`은 여전히 `False`로 유지되므로,
1E.6이 바로잡으려던 "조건식 출처 신뢰도" 의미론은 그대로 두고
**targets 산출만 8/5 이전 동작으로 복구**함 — "매매 대상 포함"과
"조건식 출처 신뢰"를 분리한 것이 이번 수정의 핵심. VWAP shadow의
condition-source 기반 판단(조건식명에 "눌림목" 포함 여부)은
영향받지 않음.

스윙 검색식이 함께 구독 중일 때는 출처 미확정 종목이 스윙 소속일
수 있어 기존 정책(제외)을 유지하되, 조용히 사라지지 않도록
`[COND] 출처 미확정 N종목이 단타 targets에서 제외됨` 경고를 남김.
(현재 `swing_condition_seqs: []`이므로 실서버에서는 항상 포함 경로.)

### P0-2: 재연결 후 CNSRREQ 900003으로 조건식이 영구 사망 (재현 확인)

**증상 (실서버 8/6 09:10:05)**:
```
ERROR [COND] 조건검색 조회 실패:
{'trnm': 'CNSRREQ', 'return_code': 900003,
 'return_msg': '이미 등록된 조건검색 일련번호입니다.(seq=3)'}
```
1E.8 이전의 재연결 루프(242회)가 서버 측 구독을 남긴 채 끊기면서,
재기동 시 seq3 등록이 900003으로 실패. `_on_initial_result()`가
early return이라 `_confirmed_symbols_by_seq["3"]`이 하루 종일 빈
채로 남았고, CNSRREQ는 연결당 1회뿐이라 자력 회복 경로가 없음
→ 해당 조건식이 통째로 죽음. P0-1과 겹치면 완전 마비.

**수정**: 900003 수신 시 `CNSRCLR`로 기존 등록을 해제한 뒤 같은
seq로 1회 재구독. 무한 루프 방지를 위해 **연결당 seq별 1회**로
제한하고(`_resubscribe_attempted`), 재로그인 시 초기화해 재연결
때마다 다시 시도할 수 있게 함. 900003 외의 오류는 기존 동작 유지.

응답에 최상위 `seq` 키가 없고 `return_msg` 문구 안에만 들어있는
실제 형식에 맞춰 `_extract_seq_from_error()`로 파싱(최상위 키 우선,
없으면 정규식). `_on_initial_result()`는 재전송을 위해 async로 전환.

### 회귀 스위트 사각지대 해소 (1E.8 과제)

`on_symbols_changed()`가 `main()` 안의 클로저라 어떤 테스트도 실행할
수 없던 구조를 바꿈:

- **`app/target_selection.py` 신규**: `compute_day_targets()` 순수
  함수 — watcher/settings 객체가 아니라 원시값만 받으므로 테스트에서
  직접 호출 가능. 반환값 `DayTargetSelection`에 `final_targets` /
  `day_symbols` / `blocked` / `unresolved_used` / `unresolved_skipped`.
- **`ConditionWatcher`에 public 접근자 추가**: `confirmed_symbols_by_seq`,
  `realtime_unresolved_symbols` (둘 다 복사본 반환 — 호출부가 내부
  상태를 오염시킬 수 없음). `app/main.py`의 private 필드 직접 참조를
  전부 제거해, 1E.8 같은 필드명 불일치 사고가 구조적으로 재발 불가.
- **`test_condition_target_selection.py` 신규 (37건)**: 8/6 실서버
  시나리오 정확 재현, 8/5 실측 규모(78종목) 상한 처리, 스윙 구독 시
  제외 정책, 제외 종목 차단, dict 순회 순서 무관 결정성, public
  접근자와 내부 상태 일치 및 복사본 보장, `app/main.py`의 private
  접근을 정규식으로 감시(`watcher\._\w+`가 0건이어야 통과), 900003
  회복 경로 전량.

테스트 작성 중 자체 검증식의 오탐을 발견해 수정함 —
`"_symbols_by_seq" not in main_src` 방식은 `confirmed_symbols_by_seq`가
`_symbols_by_seq`를 부분문자열로 포함해 항상 실패함. `watcher._\w+`
정규식으로 교체.

### 부수 수정

`test_vwap_shadow_observation.py`의 `_on_initial_result()` 호출 4곳을
`asyncio.run()`으로 감쌈(async 전환 대응). 79/79 유지.

**전체 회귀**: 12개 파일 전부 통과, 1개 스킵, 종료코드 0.
(1E.8 대비 `test_condition_target_selection.py` 1개 파일 추가)

---

## 1F: 스윙 전략 전량 폐기 — 단타 단일 구조로 정리 (2026-08-06)

**배경**: 민우님 결정으로 스윙 전략을 폐기하고 단타 전략만으로
운영하기로 함. 구조도 그에 맞춰 정리.

### 사전 확인: 스윙 파이프라인은 이미 실행 불가 상태였음

제거 전 의존 관계를 점검하다가, `domain/service/swing_service.py`가
**`SwingService` 클래스를 전혀 포함하지 않는 파일**임을 확인했습니다.
내용은 `analyze_trades.py`의 구버전(417줄 vs 현재 381줄, 800줄 차이)
으로, 어느 시점엔가 잘못된 경로에 저장된 것으로 보입니다.

따라서 `app/main_swing.py`는 188행
`from domain.service.swing_service import SwingService`에서
ImportError로 죽는 상태였고, 스윙 프로세스는 이미 기동 자체가
불가능했습니다. **이번 폐기로 실제로 잃는 동작은 없습니다.**
(`logs/app_swing.log`의 마지막 기록이 2026-06-26인 것도 이와 일치.)

### 삭제한 것

| 경로 | 내용 |
|---|---|
| `app/main_swing.py` | 스윙 프로세스 진입점 |
| `config/settings_swing.yaml` | 스윙 전용 설정 |
| `domain/swing/` | `__init__.py`, `pullback_rebound.py`, `swing_analyzer.py`, `swing_strategy.py` |
| `domain/strategy/swing_strategy.py` | 어디서도 import되지 않던 사문 |
| `domain/service/swing_service.py` | `SwingService` 없는 `analyze_trades.py` 구버전 |
| `data/swing_condition_symbols.json` | 스윙 프로세스 연동용 중간 파일 |

`domain/strategy/strategy_router.py`는 스윙을 전혀 참조하지 않아
수정 불필요 — 단타 전략 라우팅은 영향 없음.

### 코드 변경

**`app/main.py`**
- 스윙 seq 병합 제거: `dataclasses.replace()`로 `condition_seqs`에
  스윙 seq를 합쳐 `ws_config_combined`를 만들던 로직을 삭제하고,
  `ConditionWatcher`에 `settings.websocket`을 그대로 전달.
- `data/swing_condition_symbols.json` 저장 블록(`[COND_SWING]` 로그)
  제거.
- 위 블록 제거로 사용처가 사라진 `import json`,
  `from datetime import datetime` 정리(`Path`는 다른 곳에서 계속
  사용하므로 유지). 함수 내 지역 import(`_dt`, `_dt_notify`)는 그대로.

**`app/target_selection.py`**
- `swing_seqs` 매개변수와 그에 딸린 분기 제거. 스윙 검색식이 동시
  구독될 수 없게 되어, 출처 미확정 종목의 소속이 애매할 여지가
  원천적으로 사라짐 → **이제 항상 단타 targets에 포함**.
- `DayTargetSelection.unresolved_skipped` 필드 및 그 경고 로그 제거
  (발생 불가능해짐).
- `day_seqs` 필터링 자체는 **유지** — 설정에 등록되지 않은 seq의
  결과가 흘러들어와 조용히 감시 대상이 되는 것을 막는 방어선이므로
  스윙과 무관하게 남겨둘 가치가 있음.

**`domain/service/trading_service.py`**
- 스윙 전용으로 선언만 되어 있고 읽기·쓰기 참조가 **0건**이던
  `cached_weekly_bars` / `cached_weekly_bars_loaded_at` 제거
  (전체 grep으로 선언부 외 사용처 없음 확인).

**설정**
- `config/settings.py`: `WebSocketConfig`에서 `swing_condition_seqs`,
  `swing_condition_output` 필드 제거.
- `config/settings.yaml`: 해당 스윙 블록(주석 포함 7줄) 제거.
- `tests/fixtures/legacy_20260721/settings.yaml`은 **의도적으로 손대지
  않음** — 1A단계 캡처 시점을 그대로 보존해야 하는 동결 fixture이고,
  `yaml.safe_load()`로만 읽히지 `load_settings()`를 타지 않으므로
  필드 제거의 영향을 받지 않음(실제로 확인).

**테스트**
- `test_condition_target_selection.py`: 스윙 관련 3번 섹션을
  "설정에 등록되지 않은 seq의 결과가 targets에 섞이지 않음" 검증으로
  교체. 호출부에서 `swing_seqs` 인자 제거. 37건 유지, 전부 통과.
- `test_stale_sell_and_clock_safety.py`, `domain/indicator/indicators.py`:
  docstring의 스윙 언급 정리(동작 변경 없음).

### 검증

- `load_settings("config/settings.yaml")` 정상 로드,
  `hasattr(websocket, "swing_condition_seqs") == False` 확인.
- `app.main` / `app.target_selection` import 스모크 테스트 통과 —
  삭제된 모듈을 참조하는 잔존 import 없음.
- `compileall`로 `app`/`domain`/`infra`/`config`/`utils` 전체 컴파일 이상 없음.
- **전체 회귀**: 12개 파일 전부 통과, 1개 스킵, 종료코드 0.

### 남은 정리 대상(코드 아님, 다음 기회에)

`logs/app_swing.log`, `logs/swing_watch.csv`는 과거 실행 기록이라
git에 포함되지 않으며(.gitignore), 로컬에서 필요 없으면 수동 삭제
하시면 됩니다.

---

## 1G: shadow 전환 전 보완 — reliability 전달, 코드 오류 재연결 차단, 선택 통계 (2026-08-06)

**배경**: 1E.9 + 1F 누적 적용에 대한 GPT 코드리뷰에서 3건이 지적됨.
P0 타깃 누락 수정과 스윙 제거는 승인, 다만 `entry_quality_guard_mode`
를 shadow로 전환하기 전에 아래 2건을 보완할 것.

### 1. 이미 알려진 종목의 REAL I가 TradingService에 전달되지 않음 (재현 확인)

**재현 결과** (패치 전, 직접 실행):
```
변경 전 reliable: {'005930': True}
REAL I 수신
변경 후 reliable: {'005930': False}
on_symbols_changed 호출 횟수: 0
```

`_on_realtime()`의 `action == "I"` 분기는 이미 알려진 종목일 때
`_realtime_unresolved`에 추가만 하고 `_notify()`를 호출하지 않았음.
`symbol_condition_source_reliable`은 호출 시점에 재계산되므로 watcher
내부 값은 즉시 False가 되지만, `TradingService`는 `update_targets()`
콜백으로만 갱신되므로 **과거의 `condition_source_reliable=True`를
계속 사용**함 → shadow 로그에 잘못된 신뢰도가 기록될 수 있었음.

**수정**: `was_unresolved`를 먼저 저장하고, unresolved에 **처음
추가되는 경우에는 신규 종목 여부와 무관하게** `_notify()`를 호출.
동일 REAL I가 반복되면 상태 변화가 없으므로 생략(불필요한 폴링
갱신 방지). 핵심은 "종목 수가 변하지 않아도 reliability 메타데이터가
바뀌면 콜백을 호출한다"는 것.

로그도 정리 — 신규 편입은 기존 문구, 이미 알려진 종목은 신뢰도 하향
문구를 **최초 1회만** 출력(반복 수신 시 로그 폭주 방지).

### 2. 코드 오류도 무한 재연결 (재현 확인)

**재현 결과** (패치 전, 직접 실행):
```
on_message에서 AttributeError 발생시킴
→ 0.6초 동안 연결 시도 횟수: 12회
```
실서버 8/6 08:40~09:06의 242회 재연결 루프와 정확히 같은 구조.

`KiwoomWebSocket.start()`가 모든 `Exception`을 "연결 끊김"으로 취급해
5초 후 재연결했기 때문. 1E.8의 `AttributeError`처럼 **재시도해도
결과가 절대 달라지지 않는 결정적 코드 오류**까지 무한 반복됨.

**수정**:
- `MessageHandlerError(RuntimeError)` 신규 — `on_message` 콜백 내부
  예외를 이 타입으로 감싸 재전파(`__cause__`에 원래 예외 보존).
  `CancelledError`는 정상 종료 신호이므로 그대로 통과.
- `RECOVERABLE_NETWORK_ERRORS` 상수로 재연결 대상을 명시:
  `ConnectionClosed`, `OSError`(ConnectionRefused/Reset·gaierror 포함),
  `asyncio.TimeoutError`, `InvalidHandshake`, `WebSocketException`.
- `start()`의 예외 처리를 셋으로 분리 — `MessageHandlerError`는 즉시
  전파, 위 네트워크 오류만 재연결, **분류되지 않은 예외도 전파**
  (조용히 무한 반복되면 1E.8 같은 장애가 또 감춰지므로).
  새 네트워크 예외 유형이 발견되면 상수에 명시적으로 추가하는 방향.

`app/main.py`의 `watcher_start_guarded()`는 이미 예외를 재전파하고
있어 수정 불필요 — 전파된 예외가 `asyncio.wait`의 done 처리로
이어져 프로세스가 0이 아닌 종료 코드로 끝남.

### 3. target 선택 통계 로그 추가 (선택 로직은 미변경)

**정정**: 1F 완료 보고에서 "감시 종목이 4개 → 수십 개로 돌아온다"고
했는데 **부정확한 서술이었음**. `max_symbols: 10`이고 수동 targets가
4개이므로, 조건검색 종목이 차지할 수 있는 자리는 **최대 6개**이고
최종 감시 대상은 항상 10종목 이하임.

더 중요한 것은 선택 기준 — `sorted(realtime_unresolved)`로 정렬하므로
**종목코드 오름차순 앞쪽 6개**가 선택됨. 편입 시각·거래대금·점수·
상승여력 등은 전혀 반영되지 않아, 장중에 더 좋은 종목이 편입돼도
코드가 크면 잘림. 이 편향은 `entry_quality_shadow.csv` 표본에도 그대로
반영되므로 shadow 분석 시 반드시 감안해야 함.

**이번 단계에서는 선택 로직을 바꾸지 않고 편향의 크기만 관측**:
- `DayTargetSelection`에 `eligible_condition_count` /
  `selected_condition_count` / `truncated_condition_count` 추가.
  수동 targets와 겹치는 종목은 상한과 무관하게 항상 들어가므로
  조건검색 집계에서 제외(그래야 "상한 때문에 잘린 수"가 정확함).
- `[COND_STATUS]` 로그에 세 값과 `selected_symbols`(기존 `final=`)를 기록.
- 잘림이 실제로 발생하면 `[COND_TRUNCATE]` 경고를 별도로 남김.

선택 정책 개선(최근 편입 우선 / 순환 / 거래대금·점수 순위화 /
`max_symbols` 상향)은 이 로그로 실제 규모를 확인한 뒤 별도 단계에서
결정. `max_symbols`만 올리는 것은 API 호출량 측정이 선행돼야 함.

### 조건식 출처 기반 VWAP shadow의 예상 한계 (기록)

장전 초기 조회가 0종목이고 주기적 CNSRREQ 재조회가 없으므로, 장중
신규 편입 종목은 하루 종일 `condition_source_reliable=False`일 가능성이
높음. 따라서 shadow 전환 후에도 아래 4개 필드는 대부분 빈 값이 될 것:
`would_block_pullback_condition_rolling_vwap`,
`would_block_pr_or_pullback_condition_rolling_vwap`,
`would_block_pullback_condition_session_vwap`,
`would_block_pr_or_pullback_condition_session_vwap`.

MACD hard/min5, PR-only rolling/session, C-or-PR rolling/session은 정상
수집됨. 데이터 오류가 아니라 신뢰할 수 없는 값을 남기지 않는 보수적
동작이므로, **우선 MACD·PR·C 중심으로 분석**하고 조건식 출처 기반
비교는 `reliable=True` 행만 따로 모아 별도 판단.

### 테스트

`test_condition_target_selection.py` 37건 → **58건**:
- 8절(7건): 선택 통계 — 실운영 설정(max_symbols=10, 수동 4)에서
  eligible=78 / selected=6 / truncated=72 정확 집계, 수동 겹침 제외,
  자동 제외 종목 미포함, 합계 항등식.
- 9절(8건): known 종목 REAL I → 콜백 1회, 콜백 시점에 reliability=False
  전달, 반복 수신 시 콜백 생략, 신규 종목은 기존대로 콜백,
  reliability=False여도 매매 대상에는 포함.
- 10절(6건): `on_message` AttributeError → 연결 시도 1회로 종료 +
  `MessageHandlerError` 전파 + `__cause__` 보존,
  `ConnectionClosed`·`ConnectionResetError`는 기존대로 재연결.

**전체 회귀**: 12개 파일 전부 통과, 1개 스킵, 종료코드 0.
`app.main` / `app.target_selection` / `infra.websocket.kiwoom_ws`
import 스모크, `load_settings()`, `compileall` 전부 정상.

---

## 1G.1: WebSocket 예외 분류 범위 축소 + 주석 정정 (2026-08-06)

**배경**: 1G에 대한 GPT 코드리뷰에서 핵심 수정 3건은 승인됐으나,
WebSocket 예외 분류가 의도대로 좁혀지지 않았다는 지적.

### P1: `WebSocketException`이 좁히기를 통째로 무력화 (재현 확인)

1G에서 정의한 재연결 대상:
```python
RECOVERABLE_NETWORK_ERRORS = (
    ConnectionClosed, OSError, asyncio.TimeoutError,
    websockets.exceptions.InvalidHandshake,
    websockets.exceptions.WebSocketException,   # ← 문제
)
```

`WebSocketException`은 **모든 WebSocket 예외의 최상위 부모**입니다.
설치본(websockets 17.0.1)에서 직접 확인한 결과 하위 클래스가 35개이며,
`ConnectionClosed`조차 이 부모를 상속합니다. 즉 이 한 줄 때문에
사실상 "WebSocket 관련 예외는 전부 재연결"이 되어, 1G 커밋 메시지에
쓴 "명시된 네트워크 오류만 재연결하고 분류되지 않은 오류는 전파"라는
정책이 **실제로는 성립하지 않았습니다.**

**재현 결과** (수정 전, `connect()`가 각 예외를 던지도록 하고 관찰):
```
InvalidURI          연결시도 7회 → 반복 재연결
ProtocolError       연결시도 7회 → 반복 재연결
PayloadTooBig       연결시도 7회 → 반복 재연결
ConnectionClosed    연결시도 7회 → 반복 재연결
OSError계열          연결시도 7회 → 반복 재연결
```

**수정**: 재연결 대상을 "재시도하면 결과가 달라질 수 있는 일시적
오류" 셋으로 축소.
```python
RECOVERABLE_NETWORK_ERRORS = (
    ConnectionClosed,      # 서버가 연결을 끊음
    OSError,               # ConnectionRefused/Reset, socket.gaierror 등
    asyncio.TimeoutError,  # 응답 지연
)
```

`InvalidHandshake`는 서버 일시 장애일 수도, URL·인증 토큰·헤더·서버
설정 오류일 수도 있어 무제한 재연결 대상으로 두면 후자를 감춥니다.
별도 분기로 빼서 **로그를 남기고 전파**하도록 처리:
```python
except websockets.exceptions.InvalidHandshake:
    logger.exception("[WS] 핸드셰이크 실패 — URL·인증·서버 설정 오류 가능성")
    raise
```
서버 5xx에 한해 재시도하려면 상태코드 판별 + 제한 횟수 + 지수
백오프가 필요하며, 이번 단계 범위 밖으로 남깁니다.

**수정 후 재현 결과**:
```
InvalidURI          연결시도 1회 → 전파(InvalidURI)
ProtocolError       연결시도 1회 → 전파(ProtocolError)
PayloadTooBig       연결시도 1회 → 전파(PayloadTooBig)
ConnectionClosed    연결시도 7회 → 반복 재연결
OSError계열          연결시도 7회 → 반복 재연결
```

### P2-2: REAL 메시지 구조 주석 정정

`_on_realtime()` 위 주석에 "seq는 각 item의 'item' 필드에 담겨 있음"
이라고 적혀 있었으나, 바로 아래 실제 로직·주석(REAL에는 조건식 seq가
없어 출처를 확정할 수 없음)과 **정반대로 틀린 설명**이었습니다.
운영 동작에는 영향이 없었지만, 이 주석을 믿고 향후 유지보수자가
`item`을 seq로 해석해 잘못된 조건식 귀속을 다시 구현할 위험이 있어
정정했습니다. 실제 구조(2026-06-24 / 06-26 확인)를 명시:
```
data[i]['item'] = 종목코드          (조건식 seq 아님)
data[i]['name'] = '조건검색' 고정   (조건식 이름 아님)
→ REAL로 알 수 있는 것은 종목코드와 편입(I)/편출(D) 구분뿐
```

### P2-1: 중복 `return` — 해당 없음

`symbol_to_conditions` 끝의 반환문 중복이 지적됐으나, 실제 트리에서는
`return {sym: tuple(names) ...}`가 180행에 **1회만** 존재하며 파일
전체에 연속 중복 `return`도 없음을 확인했습니다(스크립트로 전수 검사).
검토 측에서 여러 단계 diff를 병합하는 과정의 아티팩트로 보입니다.
회귀 방지를 위해 이 사실을 테스트로 고정했습니다(12-1).

### 테스트

`test_condition_target_selection.py` 58건 → **73건**:
- 11절(12건): `RECOVERABLE_NETWORK_ERRORS` 구성 자체를 고정(정확히
  3종, `WebSocketException`·`InvalidHandshake` 미포함),
  `InvalidURI`/`ProtocolError`/`PayloadTooBig`/`InvalidStatus`는
  연결 1회 후 예외 전파, `ConnectionClosed`/`ConnectionResetError`/
  `asyncio.TimeoutError`는 재연결, `MessageHandlerError`는 좁힌
  뒤에도 즉시 전파 유지.
- 12절(3건): 소스 위생 — 중복 `return` 부재, 잘못된 seq 주석 부재,
  정정된 REAL 구조 설명 존재.

**전체 회귀**: 12개 파일 전부 통과, 1개 스킵, 종료코드 0.
import 스모크 / `load_settings()` / `compileall` 전부 정상.

---

## 1H: shadow 관측 데이터 분석 리포트 신규 (2026-08-06)

**배경**: shadow를 켜면 데이터는 쌓이지만 **그것을 읽는 코드가 전혀
없었음**. 확인 결과 기존 리포트 6종(`analyze_signal_log.py`,
`analyze_trades.py`, `analyze_indicators.py`, `replay_runner.py`,
`analyze_bb_block_impact`, 분봉 품질) 어디에도 `macd`/`would_block_*`/
`is_pr`/`rolling_vwap`/`session_vwap`/`chasing_overheated` 참조가
0건이고, `entry_quality_shadow.csv`를 참조하는 코드는 전부 **쓰기
쪽**(logger, trading_service, settings)뿐이었음. 매번 즉석 스크립트로
확인해야 하는 상태였음.

### `analyze_shadow.py` 신규 (읽기 전용)

기존 분석 스크립트와 동일한 패턴 — `python analyze_shadow.py
[YYYY-MM-DD] [YYYY-MM-DD]`, 콘솔 출력 + `reports/shadow_analysis_
YYYYMMDD.txt` 저장. 매매 판단·설정을 일절 바꾸지 않고 이미 기록된
CSV만 읽음.

**이번 단계 범위 — 1~4번만**:
1. **스키마·품질**: shadow 필드 채움률(`macd`/`rolling_vwap`/
   `session_metrics`), 정규장 밖 timestamp, `(종목,분봉)` 중복 행
   (재시작 중복 감지), 필수 필드 결측.
2. **표본 규모**: 행 수와 유니크 수를 **항상 나란히** 표시하고 평균
   반복 횟수를 계산. 종목·시간대 집중도로 표본 편향 점검.
3. **MACD 게이트**: 기존 `chasing_overheated` / MACD 데드+최소5점 /
   MACD>Signal 하드 게이트 3종을 유니크 기준 집계.
4. **VWAP 8조합**: PR-only / C-or-PR / condition-source /
   PR-or-condition × rolling / session.

**의도적으로 제외 — 성과 계산(5·10·20분 수익률, MFE·MAE)**:
`entry_quality_shadow.csv`에는 판단 시점 `current_price`만 있고 이후
가격이 없어 분봉 리플레이 CSV와 조인해야 함. 감시 대상에서 빠진
종목은 분봉이 수집되지 않았을 수 있어 "산출 불가" 처리 설계가
필요한데, 실제 데이터가 어떻게 쌓이는지 확인한 뒤 붙이는 편이 정확.
표본 100건도 안 되는 상태에서 성과 수치를 뽑으면 오해를 부름.

### 설계상 강조한 두 가지

**중복집계 함정**: signal_log는 폴링마다 기록되므로 같은 종목·같은
분봉이 반복됨. 8/5 실측으로 legacy BUY 후보 777행 = **유니크 212건
(평균 3.7회 반복)**. 과거 "리플레이 5228건을 독립 거래로 오해"했던
것과 같은 함정이라, 모든 집계를 `(symbol, latest_bar_timestamp)`
유니크 기준으로 하고 행 수는 참고로만 병기.

**기존 규칙 차단분 분리**: 이미 `order_block_reason`으로 막힌 후보를
분모에 섞으면 새 게이트가 막았을 건수가 부풀려짐(어차피 못 사던
후보이므로). 전체와 "기존 규칙 통과분"을 나눠서 표시.

### 8/5 실데이터 검증 결과 (즉시 드러난 사실)

```
legacy BUY 후보    777행 → 유니크 212건 / 종목 13개 (평균 3.7회 반복)
  그중 기존 규칙 통과      7건   ← 새 게이트 평가의 실제 분모
MACD>Signal 하드 게이트  전체 1건(0.5%) / 통과분 1건(14.3%)
나머지 2개 게이트         0건
```
**유니크 212건 중 기존 규칙을 통과한 것이 7건뿐**이라는 점이 핵심.
새 게이트의 실질 표본은 하루 7건 수준이므로, GPT가 제시한 기준
(유니크 100건 / 실제 진입 20건 / 종목 10개)을 채우려면 3~5거래일로도
부족할 수 있음 — 수집 기간을 늘리거나 `[COND_TRUNCATE]` 규모를 보고
선택 정책을 먼저 개선하는 판단이 필요할 수 있음.

### 장 마감 파이프라인 연결

`TradingService._run_shadow_analysis_today()` 추가 —
`_run_end_of_day_tasks()`(15:20 트리거)의 기존 6개 분석 뒤에 실행.
다른 분석과 동일하게 `subprocess` + 예외 삼킴 구조라, 분석이 실패해도
매매나 다른 리포트 생성에 영향 없음.

### 테스트

`test_shadow_analysis.py` 신규 **19건** — truthy/filled/uniq_key 유틸,
5행→유니크 2건 축약, 날짜 범위 필터링, 기존 규칙 통과분 분리,
VWAP 미수집 경고, 빈 CSV·파일 부재에서 예외 없이 동작,
파이프라인 연결 4건.

**전체 회귀**: 13개 파일 전부 통과, 1개 스킵, 종료코드 0.

### 정정 (이전 세션 안내 오류)

"`entry_quality_shadow.csv`가 헤더만이면 shadow 전환이 안 된 것"이라고
안내했으나 **틀렸음**. 이 파일은 `legacy_buy_candidate=True`일 때만
행을 기록하므로, shadow가 정상 동작해도 BUY 후보가 없으면 헤더만
남음. 정확한 확인 방법은 `signal_log.csv`의 `rolling_vwap` /
`session_metrics_ready` 채움률이며, `analyze_shadow.py`의 1번 섹션이
이 값을 그대로 보여줌.

---

## 1I: 분석용 일일 번들 export + session ready 결함 확인 (2026-08-06)

### 🔴 발견: `session_metrics_ready`가 지금까지 단 한 번도 True였던 적이 없음

8/6 shadow 데이터에서 `session_metrics_ready=0건 / PARTIAL_SESSION 358건`
이 관측돼 원인을 추적한 결과, **구조적 결함**으로 확인됨.

**ready 판정 조건** (`session_metrics.py`): 가장 오래된 세션 봉이
09:00~09:01이어야 `COMPLETE_FROM_OPEN`, 아니면 `PARTIAL_SESSION`.

**실측 (app.log `[SESSION_SHADOW]`)**:
```
024840  earliest=09:49  bar_count=331
006360  earliest=09:55  bar_count=325
005935  earliest=10:16  bar_count=304
047040  earliest=09:49  bar_count=331
```
`bar_count`가 **326~331에서 천장**을 침. 키움 분봉 API(ka10080)가
반환하는 봉 수 상한으로 보임. 09:00~15:20은 380분이므로, 거래가
활발한 종목은 **오후에는 09:00 봉이 응답에서 밀려나** ready 조건을
영구히 만족할 수 없음.

**검증**: `[SESSION_SHADOW]` 전체 2,083건 중 `ready=True` **0건**.
1C에서 `session_metrics_mode="shadow"`를 켠 2026-07-28 이후 지금까지
계속 False였음.

**영향**: `session_gate_eligible`도 전부 False라, VWAP shadow 8조합 중
**session 기준 4개가 전부 0건**으로 나옴. 8/6 리포트의
"session 계열 차단 0건"은 게이트 성능이 아니라 **데이터가 아예 생성되지
않은 것**. rolling(60분) 계열 4개는 정상 수집됨(PR-only/C-or-PR/
PR-or-condition 각 4건 차단).

**이번 단계에서는 수정하지 않음** — ready의 의미를 바꾸는 것은
shadow 해석 기준 자체를 바꾸는 결정이라 별도 단계에서 판단.
후보: (a) API 상한에 걸린 경우를 `CAPPED_BY_API`(ready=True)로
분리, (b) 세션 시작 이후 경과 시간 대비 봉 커버리지 비율로 판정,
(c) 장 시작 시점 봉을 별도로 캐시해 두고 병합. **어떤 안이든
session 계열 게이트의 과거 관측치는 전부 무효**이므로, 수정 전까지
rolling 계열만으로 분석해야 함.

### `export_daily_bundle.py` 신규

분석 때마다 `signal_log.csv`(65MB, 233,064행)와 `app.log`(9MB,
36,217줄) 전체를 올려야 했음. 해당 거래일 몫만 잘라내는 스크립트.

**실측 (8/6)**: 원본 합계 약 74MB → **번들 0.5MB**
```
signal_log.csv              9,355행 / 전체 233,064행  (3,874 KB)
entry_quality_shadow.csv      358행
entry_watch_shadow.csv        138행
app.log                    19,368줄 / 전체  36,217줄
리포트 6종 (daily_report, signal/trade/indicator/shadow_analysis, replay)
```

`exports/bundle_YYYYMMDD.zip` 하나로 묶고 `MANIFEST.txt`에 행 수·용량·
누락 파일을 기록. 수동 실행도 가능:
`python export_daily_bundle.py 2026-08-06`

**보안**: app.log는 분석에 실제로 쓰는 태그 줄만 추출 —
`[COND_STATUS]`, `[COND_TRUNCATE]`, `[COND]`, `[WS]`,
`[SESSION_SHADOW]`, `[EXPERIMENTAL]`, `[REPORT]`, `[ANALYSIS]`,
`[RECONCILE]` + ERROR/CRITICAL/WARNING 레벨. 잔고·주문응답 원문·인증
관련 줄은 태그 목록에 넣지 않아 자연히 제외되고, 혹시 섞인 계좌번호
형태(`########-##`)는 정규식으로 마스킹. `.env`/`state.json`/token
파일은 어떤 경우에도 미포함.

### 장 마감 파이프라인 연결

`TradingService._export_daily_bundle_today()` 추가 —
`_run_end_of_day_tasks()`(15:20)의 **가장 마지막**에 실행. 그날 생성된
리포트까지 번들에 담기려면 모든 분석이 끝난 뒤여야 하므로 순서가 중요.
다른 후처리와 동일하게 subprocess + 예외 삼킴.

### 부수 수정: import 시 stdout 교체 부작용

`analyze_shadow.py` / `export_daily_bundle.py`가 모듈 최상위에서
`sys.stdout`을 `TextIOWrapper`로 교체하고 있었는데, 이 모듈을
import하는 쪽(테스트 등)의 stdout까지 닫혀
`ValueError: I/O operation on closed file`이 발생함(테스트 작성 중
실제 발생). `_force_utf8_stdout()`로 분리해 **직접 실행할 때만** 적용.

### 테스트

`test_shadow_analysis.py` 19건 → **24건** (계좌번호 마스킹, 일반 줄
무변형, 인증·잔고 태그 미포함, 파이프라인 연결, 실행 순서가 shadow
분석보다 뒤인지).

**전체 회귀**: 13개 파일 전부 통과, 1개 스킵, 종료코드 0.

---

## 1I.1: 번들 날짜 정확성·보안·품질 메타데이터 보완 (2026-08-06)

### ⚠️ 1I 결론 정정 — session_metrics_ready

1I에서 다음과 같이 보고했으나 **전부 오류**였음:
- ~~"7/28 이후 ready=True가 한 번도 없었다"~~
- ~~"키움 분봉 API가 약 331봉에서 제한된다"~~
- ~~"session 기반 과거 관측치는 전부 무효다"~~

**원인**: 업로드된 `app.log` **한 개만** 보고 판단함. 로테이션된
`app.log.1` 등을 포함해 전수 집계하면:
```
전체        ready=True 3,631건 / ready=False 5,609건
2026-07-29  True   960 / False 1,017
2026-08-03  True   369 / False   401
2026-08-04  True 1,313 / False   290   ← bar_count=380, earliest=09:00:00
2026-08-05  True   182 / False 2,435
```
`bar_count=380`(09:00~15:19)인 날이 실재하므로 **331은 API 상한이
아니라** 해당 종목의 SessionState 누적 시작 시각부터 장 마감까지의
분봉 수임. 8/6이 전부 PARTIAL이었던 것은 그날 장중 재시작이 2회
있었기 때문이며, 코드 결함이 아님.

**readiness 의미는 그대로 유지**:
- 09:00 봉부터 누적 → `COMPLETE_FROM_OPEN` (ready=True)
- 장중 신규 편입·재시작 이후 누적 → `PARTIAL_SESSION` (ready=False)

ready 판정을 **완화하지 않음**. `CAPPED_BY_API` 같은 이유로
ready=True를 강제로 만들지 않음. 장중 신규 종목의 당일 전체 session
VWAP이 필요하면 "09:00~현재 분봉 backfill"이라는 별도 기능으로
풀어야 하며, 이번 단계 범위가 아님.

코드 주석·리포트 문구를 모두 정정했고, 리포트는 이제
`COMPLETE_FROM_OPEN`/`PARTIAL_SESSION` 건수와 비율을 각각 표시하며,
**session 게이트 평가에 ready=True 행만 모집단으로 사용**함. 그날
ready=True가 0건이면 "session 게이트 성과 해석 불가"를 명시하고,
"다른 날에는 정상 발생하며 코드 결함이 아니다"까지 안내함.

### entry_watch_shadow 날짜 추출 수정 + fail-closed

실제 시간 컬럼은 `trigger_at`(헤더 확인). 후보를
`("trigger_at", "timestamp", "buy_time")`로 수정.

시간 컬럼을 하나도 못 찾으면 **전체 행을 복사하던 fallback을 제거**.
일일 번들은 데이터 최소화가 목적이므로 스키마를 모르면 fail-closed가
맞음 — `SchemaError`를 올려 해당 CSV를 번들에서 제외하고 MANIFEST에
`| SCHEMA_ERROR | timestamp column not found | excluded`로 기록하며,
다른 파일 export는 계속 진행.

### app.log 추출 보안 강화

**모든 WARNING/ERROR/CRITICAL 자동 포함을 제거.** allowlist에 없는
인증·계좌·주문응답 로그가 WARNING이라는 이유만으로 번들에 실릴 수
있었음. 이제 allowlist 태그 줄만 추출:
`[COND_STATUS]`, `[COND_TRUNCATE]`, `[COND]`, `[WS]`,
`[SESSION_SHADOW]`, `[EXPERIMENTAL]`, `[REPORT]`, `[ANALYSIS]`,
`[RECONCILE]`, `[MIN_STALE]`. 1F에서 스윙을 폐기했으므로
`[COND_SWING]`은 제거.

**키 기반 마스킹**을 우선 적용 — `authorization`, `bearer`,
`access_token`, `refresh_token`, `api_key`, `secret`, `password`,
`account`/`account_no`/`account_number`, `계좌`/`계좌번호`의 **값만**
치환. 이어서 `Bearer <token>`, `########-##`, 10~13자리 연속 숫자를
형태 기반으로 마스킹. 종목코드(6자리)·날짜(8자리)·분봉 타임스탬프
(14자리)는 자릿수 경계와 `(?<!\d)`/`(?!\d)`로 회피.

### shadow 중복 판정 수정

`(symbol, latest_bar_timestamp)`만으로 중복을 판정해서, 같은 분봉에서
게이트 상태가 바뀐 **정상적인 별도 행**까지 "재시작 중복 가능"
경고로 잡혔음(8/6 리포트의 "동일 키 중복 1행 ⚠"이 실제로는 정상).

signature 로직을 `domain/shadow_signature.py` **공용 순수 모듈**로
추출해 로거와 분석기가 같은 함수를 쓰도록 함 — 복제 구현은 향후 필드
추가 시 다시 어긋날 위험이 있음. `infra/storage/logger.py`는 이제
이 모듈에서 import하며, 테스트로 `_entry_quality_shadow_key is
entry_quality_shadow_key`를 검증함.

- `entry_quality_shadow_key()` — 로거의 쓰기 시점 중복 방지 키
- `analysis_dedup_key()` — 위 키 + `order_attempted`,
  `order_accepted`, `condition_source_reliable` (사후 분석용)

리포트는 **(종목,분봉) 유니크**(표본 규모 참고용)와 **완전 동일
signature 중복**(실제 중복)을 분리 표시하고, 같은 분봉에서 상태가
바뀐 행이 있으면 "정상입니다"라고 안내함.

### 번들 구조 + 수집 품질 메타데이터

ZIP을 `reports/` · `raw/` · `metadata/`로 분리:
```
bundle_YYYYMMDD.zip
├─ MANIFEST.txt
├─ reports/  daily_report, signal/trade/indicator/shadow_analysis, replay
├─ raw/      signal_log, entry_quality_shadow, entry_watch_shadow,
│            trades, position_lifecycle, app_analysis.log
└─ metadata/ collection_quality.txt
```

`collection_quality.txt` 신규 — `process_start_count`,
`websocket_connect/reconnect_count`, `collection_status`,
`session_ready_true/false_count`와 비율, `condition_source_reliable`
비율, `shadow_to_actual_buy_coverage`, `cond_truncate_event_count`,
`max_truncated_condition_count`, `session_gate_interpretation`,
`rolling_gate_interpretation` 등. 판정은 **보수적** — 첫 데이터가
09:00~09:02이고, 장 마감까지 데이터가 있고, 장중 재시작이 없을 때만
`COMPLETE`. 그 외 `PARTIAL` 또는 `RESTARTED_PARTIAL`.

`shadow_analysis` 리포트 상단에도 품질 요약(수집 상태, shadow 주문
연결, session ready, 조건식 출처 신뢰, 해석 가능/보류 항목)을 표시.

### 원자적 ZIP 생성 + 동시 실행 보호

고정 작업 디렉터리 대신 `tempfile.mkdtemp()` 고유 디렉터리 사용.
`bundle_YYYYMMDD.zip.tmp`에 먼저 쓰고 → `testzip()` 무결성 확인 →
`fsync` → `os.replace()`로 교체. 임시 디렉터리는 `finally`에서 삭제.
동일 날짜 동시 실행은 `os.O_CREAT|os.O_EXCL` 락 파일로 방지하며,
획득 실패 시 기존 실행을 건드리지 않고 경고만 남기고 `None` 반환
(불완전 ZIP을 만들지 않음).

### 8/6 실데이터 검증 결과

```
번들 크기        0.5 MB (원본 약 74MB)
signal_log        9,355행 / 전체 233,064행
entry_quality_shadow 358행   entry_watch_shadow  3 KB (trigger_at slicing 적용)
app_analysis.log 19,267줄 / 전체 36,217줄
전일(8/5) 데이터 혼입        없음
Bearer 토큰 / 계좌번호 / access_token / authorization 원문   0건
app_analysis.log 내 10~13자리 숫자                          0건
collection_status            RESTARTED_PARTIAL (재시작 2회)
session_ready                0/2,083 → INVALID_FOR_THIS_DAY
cond_truncate_event_count    4,332회 / 최대 21종목 잘림
중복 판정                    완전 동일 0행 (이전 오탐 1행 해소)
```

`[COND_TRUNCATE]`가 하루 4,332회, 최대 21종목이 잘렸다는 사실이 새로
드러남 — GPT가 지적한 "종목코드순 앞쪽 6개 표본 편향"이 실제로 상당한
규모임. 선택 정책 개선을 shadow 5일 수집보다 먼저 검토할 근거.

### 테스트

`test_shadow_analysis.py` 24건 → **65건**:
- A(7건) 날짜 slicing — trigger_at 기준, 시간 컬럼 없는 CSV 제외 +
  SCHEMA_ERROR 기록, signal_log/trades/entry_quality_shadow 전일 행
  미포함, 스키마 오류 후에도 다른 export 계속 진행.
- B(13건) 로그 보안 — allowlist 밖 WARNING/ERROR의 토큰·계좌번호가
  번들에 없음, 일반 WARNING 줄 자체 미포함, 허용 태그는 포함,
  access_token/api_key/secret/password 마스킹, 8-2 및 10자리 계좌번호
  마스킹, 종목코드는 미마스킹, allowlist에 인증·잔고 태그 없음,
  `[COND_SWING]` 제거·`[MIN_STALE]` 추가.
- C(8건) 중복 판정 — would_block·order_accepted·final_decision·
  condition_source_reliable 변화가 각각 별도 행, 완전 동일은 중복,
  logger/analyzer 함수 동일성, 확장 관계, bool/문자열 정규화.
- D(8건) session quality — COMPLETE/PARTIAL 집계, 혼재 시 비율,
  ready=false만인 날 "해석 불가" 출력, ready=true 있으면 경고 없음,
  "한 번도 true 없음" 같은 잘못된 단정 미출력, ready 완화 미적용.
- E(8건) ZIP 안정성 — `testzip()` None, 동시 실행 시 하나만 진행,
  락 충돌·예외 시 기존 ZIP 유지, tmp·락·임시 디렉터리 정리.
- F(2건) 파이프라인 연결 및 실행 순서.

**전체 회귀**: 13개 파일 전부 통과, 1개 스킵, 종료코드 0.
`compileall` 정상.

### 남은 한계

- `trades.csv`/`position_lifecycle.csv`가 컨테이너 검증 환경에
  없어 `actual_buy_count`·`shadow_to_actual_buy_coverage`는 실서버
  첫 실행에서 확인 필요.
- 성과 계산(5·10·20분 수익률, MFE·MAE)은 여전히 미구현.
- 장중 신규 편입 종목의 session VWAP backfill 미구현 —
  재시작이 잦은 날은 session 게이트 표본이 계속 0일 수 있음.

---

## 1I.2: 로거 상태 전이 보존 · 로테이션 로그 통합 · 판정 기준 수정 (2026-08-06)

1I.1에 대한 GPT 코드리뷰 6건 반영. 매매 로직은 일절 건드리지 않음.

### P0-1: 로거가 주문 결과·신뢰도 변화를 버리고 있었음 (재현 확인)

1I.1에서 signature를 공용화했지만 **분석기만** `ANALYSIS_EXTRA_FIELDS`
를 썼고, 로거 키에는 `order_attempted`/`order_accepted`/
`condition_source_reliable`이 없었음. 분석 이전에 **로거가 먼저 행을
버리므로**, 분석기가 아무리 정확한 signature를 써도 브로커 거부 후
수락(reject→accept)이나 신뢰도 변화가 원본 CSV에 아예 존재하지 않았음.

**재현 (수정 전)**:
```
1) order_accepted=False 기록 → True
2) order_accepted=True  기록 → False (중복 오판)
3) reliable=False       기록 → False (중복 오판)
최종 CSV 행 수: 1행
```
**수정 후**: `[True, True, True, False] → CSV 3행`
(4번째 완전 동일 행만 중복으로 거부)

`STATE_TRANSITION_FIELDS`를 `entry_quality_shadow_key()`에 포함하고,
`analysis_dedup_key()`는 이를 그대로 위임 — **로거와 분석기가 완전히
같은 키**를 쓰게 됨(서로 다른 키를 쓰면 로거가 버린 행을 분석기가 볼
방법이 없음). `order_id`는 일부러 제외 — 같은 판단에서 주문을
재시도할 때마다 별도 행이 생겨 표본이 부풀려지기 때문.

### P0-2: 번들이 로테이션된 app.log.* 를 읽지 않았음

이번에 잘못된 session 결론이 나온 **직접 원인**이 로테이션 누락이었는데,
exporter도 `LOGS_DIR / "app.log"` 하나만 읽고 있었음.
`RotatingFileHandler(20MB × 백업 10개)`이므로 거래량이 많은 날은
같은 날짜 로그가 여러 파일로 나뉨.

`rotated_log_paths()` 추가 — **정확히 `app.log`와 `app.log.1`~`.10`만**
대상(`app copy.log` 같은 임의 파일은 제외). 전 파일에서 대상 날짜 줄을
모아 타임스탬프 접두사로 정렬해 하나의 `raw/app_analysis_YYYYMMDD.log`로
합침(로테이션 파일은 파일 순서가 시간순이 아니므로 정렬 필수).
MANIFEST에 `source logs:`와 파일별 줄 수를 기록.

### P1-1: 수집 완전성을 shadow 첫 기록으로 판정하면 안 됨

`entry_quality_shadow.csv`는 legacy BUY 후보가 있을 때만 기록되므로,
09:00부터 정상 수집됐어도 첫 후보가 10:30이면 PARTIAL로 오판함.

**8/6 실데이터가 정확히 그 경우였음**:
```
signal_collection_first_ts = 2026-08-06T09:00:00.203677  ← 09:00부터 정상 수집
shadow_first_candidate_ts  = 2026-08-06T10:43:45         ← 첫 BUY 후보
```
1I.1은 후자로 판정해 "PARTIAL"이라 했으나, 실제로는 **봇이 09:00부터
정상 작동**했고 그때까지 BUY 후보가 없었을 뿐임.

이제 매 폴링마다 기록되는 `signal_log.csv` 기준으로 판정:
첫 기록 ≤09:02 **AND** 마지막 기록 ≥15:15 **AND** 재시작 1회일 때만
`COMPLETE`. shadow 첫·마지막 시각은 `shadow_first/last_candidate_ts`로
coverage 정보로만 남김.

`analyze_shadow.py` 상단 문구도 정정 — 이 분석기에는 재시작 로그가
없으므로 "PARTIAL / RESTARTED" 단정을 제거하고,
`shadow 후보 관측 시작: 10:43` / `전체 수집 상태:
metadata/collection_quality.txt 참고`로 사실만 표기.

### P1-2: 거부된 매수 주문이 실제 매수로 집계됨

`side=BUY`만 세면 브로커가 거부한 주문까지 포함됨. 이제
`accepted=True`인 주문만 세고 `order_id`로 유니크 처리. 메타데이터를
같은 개념끼리 비교 가능하도록 분리:
`buy_order_attempt_count` / `accepted_buy_order_count` /
`unique_accepted_buy_order_count` / `shadow_order_attempt_count` /
`shadow_order_accepted_count` / `shadow_unique_accepted_order_count`.
coverage는 **shadow accepted ÷ trades accepted BUY**로 계산.

### P1-3: VWAP 게이트 비율 분모가 행 수였음

분자는 유니크 분봉인데 분모가 행 수라, 같은 분봉에 상태 변화 행이 2개
있으면 비율이 절반으로 낮게 나왔음. 분모도 `{uniq_key(r) for r in pool}`
로 통일.

### P2: 재연결 집계 · stale lock · 명칭

- **재연결 집계**: `"재연결" in line`은 정상 기동 로그
  `[WS] start() 진입 — 재연결 루프 시작`까지 셌음. 이제
  `"연결 끊김:" and "초 후 재연결"`인 줄만 집계. 8/6 실데이터에서
  2회 → **0회**로 정정됨(그날 실제 재연결은 없었고 재시작만 2회였음).
- **stale lock**: 프로세스가 강제 종료되면 락이 남아 이후 export가
  영구 거부됨. 락에 pid·생성시각을 기록하고 `STALE_LOCK_SECONDS`
  (30분) 초과 시 회수 후 재시도. 정상 export는 수 분 이내라 충분히
  보수적.
- **명칭**: `ready=True/False`는 로그 이벤트 행 비율이며 같은 종목이
  매분 반복되므로 독립 표본이 아님 →
  `session_ready_log_event_count/ratio`로 개명하고,
  `shadow_candidate_session_ready_count/ratio`를 별도 제공.

### 8/6 실데이터 검증

```
번들 크기                            0.5 MB   testzip() = None
collection_status                    RESTARTED_PARTIAL (재시작 2회)
signal_collection_first_ts           2026-08-06T09:00:00  ← 1I.1 오판 정정
shadow_first_candidate_ts            2026-08-06T10:43:45
websocket_reconnect_count            0  (1I.1의 2회는 기동 로그 오집계)
session_ready_log_event              0 / 2,083
shadow_candidate_session_ready       0/358 (0.0%)
cond_truncate_event_count            4,332회 / 최대 21종목
전일(8/5) 혼입                        없음
로그 내 10~13자리 숫자 / Bearer 토큰   0건 / 0건
```

### 부수 수정

`test_vwap_shadow_observation.py`의 재시작 복원 테스트가 signature
필드를 하드코딩하고 있어, 상태 전이 필드 3개 추가 시 깨졌음. 공용
목록(`ASSESSMENT_SIGNATURE_FIELDS` + `STATE_TRANSITION_FIELDS`)에서
파생하도록 바꿔 앞으로 필드가 늘어도 자동 반영되게 함. 79/79 유지.

### 테스트

`test_shadow_analysis.py` 65건 → **93건**:
- G(7건) 로거 상태 전이 — `EntryQualityShadowLogger.append_if_new()`를
  **실제로 실행**해 CSV 행 수를 셈. reject→accept / attempted 변화 /
  reliable 변화 각 2행, 완전 동일 반복 1행, 로거·분석기 키 동일,
  `order_id`는 키에 미포함.
- H(8건) 로테이션 통합 — 09시 로그를 `app.log.1`에, 15시를 `app.log`에
  두고 둘 다 포함·시간순 정렬 확인, `app copy.log` 미포함,
  MANIFEST의 source logs, ready·truncate 집계에 두 파일 모두 반영.
- I(8건) 판정 기준 — 재연결 오집계 해소, signal_log 기준 판정,
  shadow 시각 분리, COMPLETE 판정, 명칭, accepted BUY 분리 집계,
  거부 주문 제외.
- J(1건) VWAP 유니크 분모.
- K(4건) stale lock — 30분 기준, 오래된 락 회수, 갓 생성된 락은 유지.

**전체 회귀**: 13개 파일 전부 통과, 1개 스킵, 종료코드 0.
`compileall` 정상.

---

## 1I.3: JSON 토큰 마스킹 · session 해석 기준 · fail-closed 보완 (2026-08-06)

1I.2에 대한 GPT 코드리뷰 반영. 매매 로직 무변경.

### P0: JSON/dict 형태 토큰이 마스킹되지 않았음 (재현 확인)

1I.2의 마스킹 정규식은 `key=value` / `key: value` / `Bearer <token>`만
처리해서, 로그가 JSON이나 Python dict 형태이면 **토큰 원문이 그대로
남았음**.

**재현 (수정 전)**:
```
{"access_token":"SECRET123456789"}   → 그대로 노출 ⚠
{"refresh_token": "REFRESH12345..."} → 그대로 노출 ⚠
{'api_key': 'KEY123456789'}          → 그대로 노출 ⚠
{"password":"PW123","symbol":"005930"} → 그대로 노출 ⚠
{"authorization":"Bearer SECRET..."} → Bearer 정규식 덕에 우연히 마스킹
```
allowlist에 `[RECONCILE]`이 있어, 향후 reconcile 코드가 응답 dict를
로그로 찍으면 토큰이 번들에 실릴 수 있는 실질적 유출 경로였음.

**수정**: 키 양쪽 따옴표를 허용하도록 정규식을 named group으로 재작성.
구분자(`=`/`:`)와 값의 원래 따옴표는 보존해 로그 가독성 유지.
```
{"access_token":"SECRET123456789"}     → {"access_token":"***"}
{"refresh_token": "REFRESH123456789"}  → {"refresh_token": "***"}
{'api_key': 'KEY123456789'}            → {'api_key': '***'}
{"password":"PW123","symbol":"005930"} → {"password":"***","symbol":"005930"}
access_token=PLAIN123                  → access_token=***
2026-08-06 09:00:01,000 | [COND] 편입: 005930  → 변형 없음
```

### P1-1: session 게이트 해석 판정이 잘못된 모집단을 봤음

`session_gate_interpretation`을 `[SESSION_SHADOW]` **로그 이벤트 수**로
판정했는데, session VWAP 게이트 성과 분석의 실제 모집단은
`entry_quality_shadow.csv`의 **legacy BUY 후보 중 ready=True**임.
HOLD 평가에서는 ready=True가 있어도 BUY 후보에서 0건이면 분석 표본이
없는데 `AVAILABLE`이라 표시됐음.

**수정**: `sh_ready`(shadow 후보 기준) > 0일 때만 `AVAILABLE`.
로그 이벤트 기준 상태는 `session_state_collection_status`
(`COLLECTING` / `NO_COMPLETE_SESSION`)로 분리해 진단용으로 유지.

### P1-2: accepted 컬럼이 없으면 모든 BUY를 수락으로 간주했음

`_is_accepted()`가 컬럼을 못 찾으면 `True`를 반환해, 스키마가 바뀌면
거부 주문까지 체결로 집계됐음. export의 원칙이 fail-closed이므로
**모르는 주문을 체결로 추정하지 않도록** `_accepted_state()`가
`None`을 반환하게 바꾸고, 이 경우:
```
accepted_buy_order_count        = N/A(SCHEMA_WARNING)
unique_accepted_buy_order_count = N/A(SCHEMA_WARNING)
trades_accepted_schema          = SCHEMA_WARNING: accepted 컬럼 없음
shadow_to_actual_buy_coverage   = N/A
```

### P1-3: order_id가 일부에만 있으면 수락 수를 과소 집계

`buy_ids` 집합 크기로 세다가 ID 없는 주문이 통째로 사라졌음
(accepted 10건 중 8건만 ID → 8건). `_identity()`를 모듈 수준 함수로
두고, ID가 없으면 `(timestamp, symbol, side, quantity, price, index)`
로 대체. 누락 건수는 별도 경고 필드로 노출:
`accepted_buy_missing_order_id_count`,
`shadow_accepted_missing_order_id_count`.

### P2: 락 소유권 확인

30분 초과 프로세스 A의 락을 B가 회수한 뒤, A가 `finally`에서 B의
락까지 지워 C가 동시 진입할 수 있는 경계 상황이 있었음. 락 파일에
`pid + created + nonce(time_ns)` 고유 토큰을 기록하고, 해제 시
**자신이 만든 토큰과 일치할 때만** 삭제.

### 8/6 실데이터 검증

로그 끝에 `[RECONCILE] resp={"access_token":"LEAKTEST_A",
"refresh_token":"LEAKTEST_B","api_key":"LEAKTEST_C"}`를 주입해 실제
번들을 생성한 뒤 압축을 풀어 검사:
```
LEAKTEST_A / B / C 원문        0건 ✅
testzip()                      None
session_state_collection_status NO_COMPLETE_SESSION
session_gate_interpretation     INVALID_FOR_THIS_DAY  ← 1I.2 판정 정정
shadow_candidate_session_ready  0
accepted_buy_order_count        N/A (trades.csv 없음)
8/5 혼입                        없음
크기                            0.5 MB
```

### 부수 수정

`_identity()`가 사용 지점보다 뒤에 정의돼 `UnboundLocalError`가
발생했고(테스트로 검출), stale lock 회수가 `if not quiet:` 블록
안으로 들어가 quiet 모드에서 회수되지 않던 문제도 테스트로 검출해
수정함.

### 테스트

`test_shadow_analysis.py` 93건 → **117건**:
- L(11건) JSON 마스킹 — 큰따옴표/작은따옴표/공백 변형, 같은 JSON에서
  종목코드 보존, 기존 `key=value` 유지, ZIP까지 통과시킨 뒤 압축 해제
  검사로 access_token·refresh_token·api_key 원문 0건 확인.
- M(4건) session 해석 — 로그 ready=True 10건 + BUY 후보 0건이면
  `INVALID_FOR_THIS_DAY`, 로그 기준 상태는 별도 유지, BUY 후보에
  ready=True가 있으면 `AVAILABLE`.
- N(3건) accepted 스키마 fail-closed.
- O(3건) order_id 누락 처리 — 3건 전부 집계되고 누락 2건 경고.
- P(3건) 락 소유권 — stale 회수, 남의 락 미삭제, 정상 해제.

**전체 회귀**: 13개 파일 전부 통과, 1개 스킵. `compileall` 정상.

---

## 1I.4: 실제 키움 자격증명 키 마스킹 · 수집 판정 fail-closed (2026-08-06)

1I.3에 대한 GPT 코드리뷰 반영. 매매 로직 무변경.

### P0: 프로젝트가 실제로 쓰는 자격증명 키가 마스킹 목록에 없었음 (재현 확인)

코드 확인 결과 실제 사용 키:
```
infra/broker/kiwoom_broker.py:90-91  {"appkey": …, "secretkey": …}
infra/broker/kiwoom_broker.py:107    token = api_response.body.get("token")
infra/notify/kakao_notifier.py       rest_api_key, client_id, refresh_token
```

**재현 (수정 전)** — 전부 원문 유지:
```
{"token":"SECRET1"}        {"appkey":"SECRET2"}      {"secretkey":"SECRET3"}
{"app_key":"SECRET4"}      {"rest_api_key":"SECRET5"}
{"password":"hello world"} {"secret":"abc,def"}      {'secretkey': 'abc;def'}
```
`[RECONCILE]`·`[WS]`가 allowlist에 있으므로 키움 응답 dict가 로그로
나가면 토큰이 그대로 번들에 실리는 실질적 유출 경로였음.

**수정 1 — 키 목록 확장**: `token`, `appkey`, `app_key`, `secretkey`,
`secret_key`, `rest_api_key`, `client_secret`, `client_id` 추가.
`_KEY_ALT`를 **긴 키부터 정렬**해 생성 — `token`이 `access_token`보다
먼저 매칭되면 접두사만 남는 부분 매칭이 생기기 때문.

**수정 2 — 값에 공백·쉼표·세미콜론이 있어도 마스킹**: 1I.3의 단일
정규식은 값 종료 문자에 `\s , ;`가 포함돼 `{"password":"hello world"}`
같은 값을 놓쳤음. **큰따옴표 / 작은따옴표 / 무따옴표 세 패턴으로
분리**해, quoted는 닫는 따옴표까지를 값으로 보고 unquoted만 구분자에서
끊도록 함.

```
{"token":"SECRET1"}         → {"token":"***"}
{"appkey":"SECRET2"}        → {"appkey":"***"}
{"password":"hello world"}  → {"password":"***"}
{"secret":"abc,def"}        → {"secret":"***"}
{'secretkey': 'abc;def'}    → {'secretkey': '***'}
{"access_token":"A","symbol":"005930"} → {"access_token":"***","symbol":"005930"}
2026-08-06 09:00:01,000 | [COND] 편입: 005930          → 변형 없음
```

### P1: `process_start_count=0`이어도 COMPLETE로 판정됐음 (재현 확인)

기동 로그가 0건이면 판정 근거 자체가 없는데 `signal_log` 범위만 맞으면
`COMPLETE`가 됐음. 보고한 기준("정확히 1회 기동")과 불일치.
`UNKNOWN_START_PARTIAL`을 신설해 구분:
```
starts == 0 → UNKNOWN_START_PARTIAL
starts > 1  → RESTARTED_PARTIAL
starts == 1 and open_ok and close_ok → COMPLETE
그 외        → PARTIAL
```

### P1: accepted 빈 값·부분 인식 처리

- 빈 문자열은 "명시적 거부"가 아니라 **미상**이므로 `False`가 아니라
  `None`. 알 수 없는 값도 동일. `_TRUE`/`_FALSE` 목록을 명시하고
  둘 다 아니면 `None`.
- `any(...)` → `all(...)`: 한 건이라도 판정 불가면 스키마를 신뢰하지
  않음(fail-closed). `buy_accepted_state_missing_count` 추가.

### P1: order_id 누락 시 unique 수는 N/A

1I.3의 fallback 키에 `idx`가 들어가 **완전히 동일한 중복 행도 별개
주문**으로 세어졌음 — `unique_accepted_buy_order_count`라는 이름과
맞지 않음. 분석 메타데이터에서는 보수적 방식을 택해, `order_id`가
하나라도 없으면 unique 수와 coverage를 `N/A(order_id 누락 있음)`로
처리하고 행 수(`accepted_buy_order_count`)만 표시.

### P2: 락 PID 생존 확인 + 고유 tmp 경로

- `_lock_owner_alive()` 추가 — 30분이 지나도 **소유 PID가 살아 있으면
  회수하지 않음**. `os.kill(pid, 0)`으로 확인하며, 판정 불가
  (`PermissionError`/`OSError`) 시에는 보수적으로 "살아 있다"로 봄.
  PID 형식을 못 읽으면 구버전 락으로 보고 회수 허용.
- 임시 ZIP 이름에 `pid + time_ns`를 붙여 동시 실행 시 서로의 임시
  파일을 덮어쓰지 않게 함.

### 8/6 실데이터 검증

실제 키움 응답 형태를 로그에 주입하고 번들을 만든 뒤 압축 해제 검사:
```
주입: {"appkey":"LEAK_APPKEY","secretkey":"LEAK_SECRETKEY"}
      {"token":"LEAK_TOKEN","access_token":"LEAK_AT","refresh_token":"LEAK_RT"}
      password="LEAK PW WITH SPACE" api_key=LEAK_APIKEY

결과: 7종 전부 0건 ✅   testzip() = None
      collection_status           RESTARTED_PARTIAL (기동 2회)
      session_gate_interpretation INVALID_FOR_THIS_DAY
      accepted_buy_order_count    N/A (trades.csv 없음)
      8/5 혼입                     없음        크기 0.5 MB
```

### 테스트

`test_shadow_analysis.py` 117건 → **157건**:
- Q(17건) 실제 자격증명 키 — 8개 키가 목록에 포함, JSON/dict 형태
  마스킹, 공백·쉼표·세미콜론 포함 값, 종목코드·타임스탬프 보존,
  긴 키 우선 매칭, ZIP 통과 후 appkey/secretkey/token 원문 0건.
- R(4건) 수집 상태 — 기동 1회 COMPLETE, 0회
  `UNKNOWN_START_PARTIAL`(+`full_day_collection=False`), 2회
  `RESTARTED_PARTIAL`.
- S(4건) accepted 빈 값 → SCHEMA_WARNING + 판정 불가 건수,
  전부 인식 시 정상 집계.
- T(3건) order_id 누락 시 unique N/A, 행 수는 보존, coverage N/A.
- U(5건) 락 — 살아 있는 PID는 회수 금지, 죽은 PID는 회수,
  고유 tmp 경로, `_lock_owner_alive` 동작.

1I.3에서 추가한 O-1은 정책이 바뀌어 갱신함(3건 집계 → N/A + 행 수).

**전체 회귀**: 13개 파일 전부 통과, 1개 스킵. `compileall` 정상.

---

## 1I.5: Windows fsync 호환 핫픽스 (2026-08-06)

**증상 (실서버 PowerShell)**:
```
File "export_daily_bundle.py", line 646, in build
    os.fsync(f.fileno())
OSError: [Errno 9] Bad file descriptor
```
번들 생성이 마지막 단계에서 실패해 `bundle_YYYYMMDD.zip`이 만들어지지
않음.

**원인**: 1I.1에서 원자적 ZIP 생성을 넣으면서 `open(tmp_zip, "rb")`로
연 **읽기 전용 핸들**에 `os.fsync()`를 호출했음. Linux는 이를 허용하지만
Windows는 `FlushFileBuffers`가 쓰기 권한을 요구해 EBADF로 실패함.
컨테이너(Linux)에서만 검증해 놓친 플랫폼 차이.

**수정**:
- `open(tmp_zip, "r+b")` — 쓰기 가능한 모드로 열고 `flush()` 후 `fsync()`.
- 플러시 실패를 `OSError`로 잡아 **경고만 남기고 계속 진행**. 무결성은
  바로 위 `testzip()`에서 이미 확인하므로, 플러시는 보강일 뿐 번들
  생성의 성공 조건이 아님. 여기서 죽으면 리포트 파이프라인 전체가
  헛돌게 됨.

**테스트** `test_shadow_analysis.py` 157건 → **162건**:
V(5건) — `r+b` 사용 및 `rb` 부재를 소스 수준에서 고정,
`os.fsync`가 `OSError(9)`를 던지도록 패치한 상태(Windows 상황 모사)에서
최종 ZIP이 정상 생성되고 `testzip()` 통과, tmp 파일 미잔존.

**전체 회귀**: 13개 파일 전부 통과, 1개 스킵.

**교훈**: 이 프로젝트는 실행 환경이 Windows/PowerShell인데 검증은
Linux 컨테이너에서 이뤄짐. 파일 핸들·경로·인코딩처럼 플랫폼 의존성이
있는 코드는 앞으로 실패 경로를 명시적으로 테스트할 것.

---

## 1J: 거래 비용 모델 단일 출처화 (2026-08-07)

### 배경 — 비용이 두 갈래로 갈라져 있었음

```
infra/storage/daily_reporter.py   COST_RATE = 0.009  (0.90%)
replay_runner.py                  0.25 + 0.10 = 0.35%
analyze_crash_rebound_days.py     0.35%
analyze_v_drop_backtest.py        0.35%  (replay에서 import)
simulate_pullback_removal.py      0.35%
```
2026-07에 `COST_RATE`를 0.53% → 0.90%로 정정했을 때 백테스트
스크립트 4개가 따라가지 못한 결과. 0.55%p 차이는 작지 않음 —
8/7 승리 거래 평균이 +0.28%였으므로 0.35% 기준의 "승리"가 0.90%
기준에서는 전부 적자가 됨. **지금까지의 백테스트 결론이 비용을
과소평가한 상태에서 나왔을 가능성**이 있음.

### 설계 — 하나로 고정하지 않고 세 시나리오를 함께 계산

실제 체결 비용이 얼마인지 아직 검증되지 않았으므로, 하나를 고르면
그 선택이 모든 결론에 숨은 가정으로 박힘. 대신 항상 셋을 함께 냄:

```
Gross  0.00%   비용 전 원수익 — 신호 자체의 품질
Base   0.35%   왕복 수수료+세금+슬리피지 추정치
Stress 0.90%   보수적 상한 (daily_report가 쓰던 값)
```
Base와 Stress가 같은 방향이면 견고한 결론, 부호가 갈리면 비용
가정에 의존하는 취약한 결론이라는 뜻.

**실제 `trades.csv` 손익은 비용 전 raw PnL로 보존**. 비용은 분석
단계에서만 더함 — 원본에 섞으면 나중에 Base를 교정할 때 되돌릴 수
없음(테스트 8-1로 고정).

### `domain/cost_model.py` 신규

`CostModel` 데이터클래스 + `load_cost_model()`. 제공 API:
`cost_pct(scenario)` / `net(gross, scenario)` / `net_all(gross)` /
`positive_rates(returns)` → `gross_positive_rate`,
`base_net_positive_rate`, `stress_net_positive_rate` /
`describe()` / `format_scenarios()`.

설정은 `config/settings.yaml`의 `cost_model` 블록 단일 출처.
설정 파일이 없으면 모듈 상단 기본값 사용 — 분석 스크립트가
프로젝트 루트 밖에서 실행돼도 통째로 죽지 않도록.

### 적용

- `replay_runner.py`, `analyze_crash_rebound_days.py`,
  `simulate_pullback_removal.py` — 하드코딩 제거, `load_cost_model()`
  참조. `TOTAL_COST_PCT`는 Base를 가리키는 하위호환 별칭으로 유지
  (`analyze_v_drop_backtest.py`가 replay에서 import하므로 간접 반영).
- `infra/storage/daily_reporter.py` — `COST_RATE`를
  `stress_roundtrip_pct / 100`으로 대체.
- **기준 차이를 리포트에 명시**: daily_report에
  `※ 비용 기준: Gross 0.00% / Base 0.35% / Stress 0.90% — 이 리포트는
  보수적 상한인 Stress를 적용합니다` +
  `※ replay/백테스트 리포트는 Base 기준이므로 수치가 다릅니다`.
  replay 쪽에도 대응 문구 추가.
- replay 출력에 시나리오 3줄 추가:
```
   5분 후  승률 5/6 (83%)  평균 +0.71%  →  비용 후 +0.36%
           Gross  +0.71%   플러스비율 83%
           Base   +0.36%   플러스비율 83%
           Stress -0.19%   플러스비율 33%
```
7/31 실데이터에서 **Base 83% → Stress 33%로 플러스비율이 뒤집히는
것**이 바로 드러남 — 비용 가정이 결론을 얼마나 좌우하는지 보여주는
사례.

### 테스트

`test_cost_model.py` 신규 **35건** (GPT 지정 6항목 전부):
1. 동일 gross return에 Gross/Base/Stress 정확 계산 + 승률 3종 +
   None 처리 + 잘못된 시나리오명 ValueError.
2. 분석기 5종이 모두 같은 cost model 참조(직접/간접).
3. 정규식 전수 검사로 허용 위치(`domain/cost_model.py`,
   `settings.yaml`, 테스트, CHANGELOG) 밖에 `0.25`/`0.10`/`0.009`
   하드코딩 0건.
4. 설정 변경 시 모든 결과가 함께 변경 + 설정 부재 시 기본값.
5. `Gross < Base < Stress` 및 순수익 역순 관계.
6. 기존 계산 재현 — Base가 replay의 0.35%, Stress가 daily_report의
   0.90%와 일치, `+0.71% → +0.36%` 재현.
7. 리포트에 기준 차이 명시 여부.
8. `trades.csv` raw PnL 보존(기록기가 비용을 차감하지 않음).

**전체 회귀**: 14개 파일 전부 통과, 1개 스킵. `compileall` 정상.
**BUY/HOLD/SELL 로직 무변경** — 비용 설정 정리만.

### 남은 것 (별도 단계)

실제 체결 기준 P&L 정확도 — 8/7 raw에서 `005930`의 BUY 주문가
238,500원과 broker `avg_buy_price` 239,000원이 달랐음. 현재
daily_report의 "체결가 기준" 표현이 부정확할 수 있으므로,
`filled_quantity` / `fill_price` / `average_fill_price` /
`commission` / `tax`를 실제 체결 이벤트에서 저장하는 작업이 필요.
그 전까지는 "주문가 기준 예상손익"으로 표기하는 것이 정확함.

---

## 1J.1: 비용 모델 신뢰성 마무리 — fail-closed 로딩 (2026-08-07)

1J에 대한 GPT 코드리뷰 5건 반영. 매매 로직 무변경.

### 1. 설정 로딩을 fail-closed로 (재현 확인)

**재현 (수정 전)**:
```
cwd를 프로젝트 밖으로 변경 → 예외 없이 Base 0.35 반환 ⚠
YAML이 깨져 있음          → 예외 없이 Base 0.35 반환 ⚠
```
기본 경로가 `Path("config/settings.yaml")`이라 **cwd에 의존**했음.
나중에 `base_roundtrip_pct: 0.42`로 교정했는데 Windows 작업
디렉터리가 달라 설정을 못 찾으면, 51일 분석이 아무 경고 없이 다시
0.35%로 돌아감 — **1J를 한 의미가 사라짐.**

**수정**:
- `PROJECT_ROOT = Path(__file__).resolve().parents[1]` 기준 절대경로
  (`DEFAULT_SETTINGS_PATH`). 상대경로가 주어졌고 cwd에 없으면
  프로젝트 루트 기준으로 한 번 더 시도.
- `CostModelConfigError` 신규. 설정 누락 / YAML 파싱 실패 /
  `cost_model` 블록 누락 / 필수 키 누락 / 숫자 변환 실패 /
  `Gross < Base < Stress` 위반 / 음수 비용 — **전부 예외**.
- 기본값은 `allow_default=True`를 명시할 때만. 분석기는 기본 strict.

### 2. 경로별 cache (재현 확인)

**재현 (수정 전)**: 전역 `_CACHED` 하나라 A→B 순서로 읽으면
`A = 0.11/0.22`, `B = 0.11/0.22`(잘못됨). 1K에서 여러 설정으로
실험할 수 있으므로 `_CACHE: dict[Path, CostModel]`로 분리.
**수정 후**: `A → 0.11/0.22`, `B → 0.77/0.88`.

### 3. 분석기 3시나리오 출력 통일

1J에서 세 시나리오를 실제로 출력하는 건 `replay_runner`뿐이었음.
"비용 가정에 따른 결론 뒤집힘을 항상 노출한다"는 1J 원칙에 맞춰
나머지도 통일:
- `CostModel.scenario_lines()` 공용 헬퍼 — 평균 + 플러스비율 3줄.
- `analyze_crash_rebound_days.py` / `analyze_v_drop_backtest.py` /
  `simulate_pullback_removal.py`에 `append_cost_scenarios()` 추가.
- 결과 dict에 `gross_5m` / `base_5m` / `stress_5m` 병행 산출.
  `net_5m`은 **Base alias**로 유지(하위호환 — 기존 집계 코드가
  `net_5m`을 참조).

### 4. roundtrip_pct 기준금액 통일

replay는 `gross_return_pct - cost_pct`인데 daily_report는
`매도금액 × cost_pct`였음. 매수 100 → 매도 110이면 매도금액 기준
비용은 진입원금 대비 **0.99%**가 되어 "0.90%"와 어긋남.

정의를 고정: **`roundtrip_pct` = 진입 원금(buy notional) 대비 왕복
총비용 추정률**.
```
replay:  net_return_pct = gross_return_pct - cost_pct
daily :  estimated_cost = buy_notional × cost_pct / 100
```
- `daily_reporter`의 `sell_price × qty` → `buy_price × qty`.
- 이월 포지션은 진입 원금을 확정할 수 없으므로 `avg_buy_price × qty`
  를 추정치로 쓰고 **리포트에 추정치임을 명시**.
- `CostModel.cost_amount(buy_notional, scenario)` 추가.
- 리포트 표기: `추정 비용 (Stress 0.90%, 진입 원금 기준)`.

**부수 정정**: GPT가 지적한 P&L 표기 문제도 함께 처리 — 8/7
`005930`의 BUY 주문가 238,500원과 broker `avg_buy_price` 239,000원이
달랐으므로 "체결가 기준"은 부정확. **"주문가 기준 예상"**으로 변경.
실제 체결 이벤트(`filled_quantity`/`fill_price`/`commission`/`tax`)
저장은 별도 단계.

### 5. 하드코딩 검사 강화

1J의 검사는 과거 변수명만 봐서 `BACKTEST_COST = 0.35` 같은 새
하드코딩을 못 잡았음. 이제 **비용 literal 자체**
(`0.35`/`0.90`/`0.9`/`0.0035`/`0.009`)를 `.py`·`.yaml` 전체에서
찾고, 허용 위치(`domain/cost_model.py`, `settings.yaml`,
`test_cost_model.py`, CHANGELOG)만 제외. 주석 줄은 설명일 수 있어
오탐 방지로 제외하며, **검사기가 새 변수명을 실제로 잡는지**도
자기검증(3-2, 3-3).

### 테스트

`test_cost_model.py` 35건 → **68건**:
- 9절(9건) fail-closed — cwd 밖에서도 정상 로드, 절대경로 확인,
  설정 누락/YAML malformed/블록 누락/키 누락/숫자 변환 실패/
  순서 위반/음수 비용 각각 예외.
- 10절(3건) 경로별 cache — A/B 순차 로딩이 서로 오염되지 않음.
- 11절(8건) 기준금액 — `cost_amount`가 진입 원금 기준,
  replay 정의와 일치(`10% → 9.10%`), daily_report가 매수금액에
  비용률을 곱하는지, 이월 추정치 명시, "주문가 기준 예상" 표기.
- 12절(9건) 분석기 3시나리오 출력 통일.
- 3절 강화 및 4-4(정책 변경: `allow_default` 명시 시에만 기본값).

**전체 회귀**: 14개 파일 전부 통과, 1개 스킵. `compileall` 정상.

---

## 1J.2: 실행 경로 결함 수정 + 실행 기반 테스트 전환 (2026-08-07)

1J.1에 대한 GPT 코드리뷰 반영. **68/68이 통과했는데도 실제 실행에서
깨지는 결함이 있었고, 원인은 테스트가 소스 문자열만 검사한 것**입니다.

### 근본 원인 — source-text test의 false pass

```python
"append_cost_scenarios" in src          # 정의만 있어도 통과
'"gross_5m"' in src or "COST_MODEL.net(" in src   # Base 하나만 있어도 통과
```
"함수가 존재한다"를 검사했지 "실제 출력이 맞다"를 검사하지 않았음.
12절 전체를 실행 기반으로 교체했습니다.

### P0-1: `analyze_v_drop_backtest.py` NameError (재현 확인)

```
has COST_MODEL: False
simulate_event가 COST_MODEL 사용: True
→ NameError: name 'COST_MODEL' is not defined
```
`from replay_runner import load_bars, TOTAL_COST_PCT`만 가져왔는데
`simulate_event()`는 `COST_MODEL.net(...)`을 씀. 다른 분석기와
동일하게 `load_cost_model()`을 직접 호출하도록 변경하고
`TOTAL_COST_PCT`는 Base alias로 유지.

### P0-2: helper가 정의만 되고 호출 0회 (재현 확인)

```
analyze_crash_rebound_days.py  append_cost_scenarios 1회 (= 정의)
analyze_v_drop_backtest.py     1회 (= 정의)
simulate_pullback_removal.py   1회 (= 정의)
```
"Gross/Base/Stress를 함께 출력한다"는 1J.1 보고와 코드가 달랐음.
실제 리포트 생성 경로에 연결:
- **crash**: 지연별(`저점+3/5/10분`) + `저점 즉시진입 → 종가`
- **v-drop**: horizon별(`5/10/20분 후`) Base 요약 바로 아래
- **pullback**: `summarize()`에서 horizon별

수정 후 등장 횟수 — crash 3회 / v-drop 2회 / pullback 2회
(정의 1 + 호출 N).

**실행 확인** (51거래일 실데이터):
```
저점+3분 진입 → 종가   1061건  승률 872/1061 (82%)  평균 +4.95%
  [ 저점 후 3분 진입 ] 비용 시나리오별 (n=1061)
           Gross  +5.30%   플러스비율 86%
           Base   +4.95%   플러스비율 82%
           Stress +4.40%   플러스비율 76%
```

### P1: 3시나리오 필드 통일

- **crash**: `entries`에 `gross_return`/`base_return`/`stress_return`
  추가, `net_return = base_return` alias.
- **pullback**: `row_base`에 `gross_{5,10,20}m`/`base_*`/`stress_*`
  추가, `net_* = base_*` alias(주석 명시).
- **v-drop**: 이미 있던 필드를 리포트에서 실제 사용.

### P1: Gross 정의 강제 (재현 확인)

`CostModel(0.10, 0.35, 0.90).validate()`가 통과해 "Gross 0.10%"라는
모순된 출력이 가능했음. Gross는 정의상 비용 전 원수익이므로
`abs(gross) > 1e-9`면 `CostModelConfigError`.

### P1: daily_report 중복 문구 제거

`※ 비용 기준...` / `※ replay/백테스트...` 두 줄이 2회 출력되던 것을
1회로. pullback의 `비용 가정: 왕복 0.35%` 잔여 줄도 제거(시나리오
줄로 대체).

### P1: allow_default 정책 명확화

의도를 **"설정 파일 자체를 사용할 수 없을 때만 fallback"**으로 확정:
```
허용(기본값): 파일 누락 / YAML 파싱 실패 / cost_model 블록 부재
불허(항상 예외): 키 누락 / 숫자 변환 실패 / 검증 위반(순서·음수·Gross≠0)
```
앞의 셋은 "설정을 못 읽었다"이고 뒤의 셋은 "설정이 있는데 틀렸다"라,
후자를 기본값으로 덮으면 사용자 의도가 조용히 무시되기 때문.
docstring과 테스트(14절)를 이 정책에 맞춤.

### 부수 수정 2건

**import 시 stdout 교체** — `replay_runner`, `simulate_pullback_removal`,
`analyze_signal_log`, `analyze_trades`, `analyze_indicators`가 모듈
최상위에서 `sys.stdout`을 교체해, import하는 테스트의 stdout까지 닫혀
`ValueError: I/O operation on closed file`이 발생했음(1I.5와 동일
패턴). `_force_utf8_stdout()`로 분리해 `main()`에서만 호출.

**`data/minute_bars/rejected/`** — `simulate_pullback_removal.py`가
날짜가 아닌 디렉터리에서 `int()` 변환 실패로 죽었음(기존 결함).
8자리 숫자 디렉터리만 대상으로 필터. **1K에서 51일 리플레이를 돌릴
때 바로 막혔을 문제.**

### 테스트

`test_cost_model.py` 68건 → **88건**:
- **12A(6건)** v-drop 실행 — `load_bars`를 `MinuteBarRow` 호환
  fixture로 monkeypatch하고 `simulate_event()`를 **실제 호출**.
  NameError 부재, `gross/base/stress` 필드 산출,
  `net_5m == base_5m`, `Gross > Base > Stress` 수치 관계.
- **12B(15건)** helper 실행 — 세 분석기의 `append_cost_scenarios()`를
  직접 호출해 출력 문자열에 Gross/Base/Stress·플러스비율이 있는지,
  세 값의 대소 관계와 `Gross - Base == base_roundtrip_pct`를 검증.
- **12C(3건)** helper 호출 존재 — "정의 1회 + 호출 1회 이상"을 확인해
  1J.1의 false pass 재발 차단.
- **12D(5건)** 3시나리오 필드 및 Base alias 명시.
- **13절(4건)** Gross==0 강제 — `0.10`/`-0.10` 예외, `0.00` 정상,
  설정 경유 로딩도 예외.
- **14절(6건)** allow_default 정책 일치.
- **15절(2건)** daily_report 중복 문구 제거.

**전체 회귀**: 14개 파일 전부 통과, 1개 스킵. `compileall` 정상.
**실행 스모크**: replay / crash / v-drop / pullback 4종 모두 정상 실행.
**BUY/HOLD/SELL 로직 무변경.**

---

## 1J.3: Replay Time-Axis Integrity (2026-08-07)

1J.2에 대한 GPT 코드리뷰에서 **비용 모델보다 중요한 replay 시간축
결함**이 발견됨. 51거래일 결과를 왜곡하는 P0.

### 실측 재현 (분봉 CSV 전수)

```
분봉 CSV                1,781개
대상일 외 날짜 봉 포함     920개 (51.7%)
1분 초과 gap 존재         821개 (46.1%)
5분 이상 gap 존재         272개 (15.3%)
예: data/minute_bars/20260623/005930.csv 첫 봉 = 20260622142200
```
전일 꼬리 봉이 섞이는 것 자체는 정상(실시간 봇이 최근 60봉을 받으며
전일 봉을 포함). 문제는 replay가 이를 다루는 방식이었음.

### `domain/replay_context.py` 신규 — 공용 시간축

네 분석기가 각자 구현하지 않고 `ReplayDayContext` 하나를 공유:
`is_target_bar` / `target_indices` / `target_bars` / `previous_close` /
`analysis_window` / `price_at_horizon` / `bars_between` / `mfe_mae` /
`index_at_or_after`. 봉 클래스가 파일마다 달라(`close_price` vs
`close`) 필드명 흡수 헬퍼도 포함.

### P0-1: 전일 봉이 entry candidate로 평가됨

`for i in range(5, len(bars))`가 전일 봉까지 순회 → 6/23 replay가
6/22 봉에서 BUY 신호를 만들 수 있었음. **candidate는 `cntr_tm`
날짜가 target_date인 봉으로 한정**. 전일 봉은 history로만 사용.
실데이터 검증: 409봉 → target 349봉(전일 60봉 제외).

### P0-2: live 60봉 vs replay 누적 전체

live는 `broker.get_minute_bars(count=cfg.minute_bar_count)`(=60)를
`MinuteAnalyzer`에 넘기는데 replay는 `bars[:i]`라 오후에 200~400봉이
들어감. `MinuteAnalyzer`가 전달된 bars 전체로 VWAP·day_high·day_low를
계산하므로 **다른 전략**이었음. `analysis_window()`가
`bars[:i+1][-60:]`을 돌려주도록 통일.

### P0-3: 현재봉 누락 (1봉 시차)

`window=bars[:i]`, `current=bars[i]`라 analyzer는 i-1까지만 보고 i
가격으로 진입. live는 최신 현재봉을 포함한 60봉을 분석. 이제
현재봉이 항상 `window[-1]`.

### P0-4: prev_close가 파일 첫 봉

`bars[0].close_price`는 전일 *첫 저장봉*이지 전일 종가가 아님.
등락률 A조건에 직접 들어가는 값. **target_date 이전 마지막 봉의
close**로 교정. 실데이터: 353,500(정확) vs 353,750(기존).
이전 날짜 봉이 없으면 **임의 추정하지 않고 None** — replay는 해당
종목을 skip하고 `PREV_CLOSE_UNAVAILABLE`에 기록.

### P0-5: "5분 후"가 실제로는 "5개 봉 후"

`idx = i + m`. gap이 46.1% 파일에 있어 8분·15분·35분 후가 될 수
있었음. **timestamp 기준으로 교체** — `entry_dt + m분` 시각 이하의
가장 최신 봉을 mark-to-market으로 쓰되, `MAX_STALENESS_MINUTES`(3분)
이상 오래된 봉이면 N/A. `actual_elapsed_minutes`를 함께 기록해
`idx+m`을 "m분 후"라고 부르지 않음. 날짜 경계를 넘지 않음.

MFE/MAE도 `bars[i+1:i+21]`이 아니라
`entry_dt < bar_dt <= entry_dt + 20분` 범위.

### P0-6: v-drop 날짜 비교

`if bar_time >= entry_ts.time():`로 **날짜를 버리고 시각만 비교** —
6/23 09:23 이벤트인데 파일 첫 봉이 6/22 14:22면 "14:22 >= 09:23"이라
**전날 봉이 진입봉으로 선택**될 수 있었음. 또 로그 timestamp
`09:23:00.608` vs 분봉 `09:23:00` 때문에 09:24로 밀렸음.
`index_at_or_after()`가 full datetime + 분 단위 내림으로 매칭.

### P0-7: crash / pullback 동일 적용

- **crash**: `day_open`/`day_low`를 target_date 봉만으로 계산.
  저점+3/5/10분도 clock-time 기준.
- **pullback**: candidate 한정 + 60봉 window + 현재봉 포함 +
  prev_close + timestamp horizon 전부 공용 컨텍스트로.

### 실측 영향 — 지적보다 컸음

```
crash 3분 지연 Gross 평균
  1J.2 (기존 시간축)   +5.30%
  1J.3 (교정 후)       +3.84%      ← 1.46%p 차이
급락일 판정            50일 → 49일
표본                   1061건 → 1049건
```
GPT 추정 0.79%p보다 큽니다(날짜 경계 + clock-time 둘 다 반영했기
때문). **지금까지의 51일 replay/backtest 수치는 enforce 판단에
사용하지 않고 전부 재산출합니다.** Gross/Base/Stress 비용 민감성
구조 자체는 유효.

### P1: 1J.1 invariant 복구 (누적)

1J.2에서 5~8절을 12절로 **교체**해버렸음 — 88 > 68이지만 누적이
아니었음. 다음을 복구:
`Gross < Base < Stress`, 순수익 역순, `SCENARIO_ORDER` 고정,
Base=0.35 / Stress=0.90 재현, `+0.71 → +0.36` 재현, daily/replay
기준 문구, trades logger의 raw PnL 보존.

### P1: simulate_event false-pass 제거

`_sim_err is None or "NameError" not in _sim_err`는 KeyError·
TypeError·IndexError가 나도 PASS했고, `_sim`이 dict가 아닐 때
"예외가 있으면 PASS"하는 구조였음.
→ `check(예외 없음, _sim_err is None)` +
`check(dict 반환, isinstance(_sim, dict))`로 분리.

### 테스트

- **`test_replay_time_axis.py` 신규 67건** — A(전일 봉 candidate
  배제 7건) / B(60봉 window·현재봉 포함 17건) / C(prev_close 5건) /
  D(timestamp horizon 7건) / E(clock-time MFE·MAE 4건) /
  F(v-drop 날짜·초 매칭 5건) / G(분석기 4종이 공용 컨텍스트 사용
  14건) / H(실데이터 회귀 5건).
  G절은 주석·docstring 오탐을 피하려고 **AST로 코드만 추출**해 검사
  (작성 중 실제 오탐 발생).
- **`test_cost_model.py` 88건 → 101건** — 삭제된 invariant 복구 +
  false-pass 제거.

**전체 회귀**: 15개 파일 전부 통과, 1개 스킵. `compileall` 정상.
**실행 스모크**: replay / crash / v-drop / pullback 4종 정상.
**BUY/HOLD/SELL 실매매 로직 무변경.**

---

## 1J.3.1: Replay 데이터 품질 계측 + fail-closed 마무리 (2026-08-07)

1J.3에 대한 GPT 코드리뷰 반영. 매매 로직 무변경.

### 1. pullback MFE/MAE만 아직 "20개 봉"이었음 (재현 확인)

5/10/20분 수익률은 1J.3에서 clock-time으로 고쳤는데
`simulate_pullback_removal.py`의 `future = bars[i + 1: i + 21]`만
남아 있었음. gap이 46.1% 파일에 있으므로 20분을 훌쩍 넘는 구간이
MFE/MAE에 포함됐음. `ctx.mfe_mae(entry_dt, entry_price, minutes=20)`
으로 통일. **"4개 분석기 모두 전환 완료"라는 1J.3 보고는 이 부분에서
부정확했습니다.**

### 2·3. replay의 silent fallback — 1J.1에서 제거한 문제의 재발

```python
try:
    MINUTE_BAR_COUNT = load_settings().market_regime.minute_bar_count
except Exception:
    MINUTE_BAR_COUNT = 60          # ← 비용 모델에서 제거했던 바로 그 패턴
```
설정이 `minute_bar_count=90`으로 바뀌어도 로딩이 실패하면 51일
백테스트가 아무 경고 없이 60봉으로 돌아감.

더 위험한 것은 analyzer:
```python
except Exception:
    return None          # → replay가 A(simple) 간이 전략으로 51일 완주
```
백테스트가 **실패하지 않고 다른 전략 결과를 내놓는** 형태.

**수정**:
- `ReplayConfigError` 신규. 설정 로딩 실패 / `minute_bar_count`
  누락·비정수·0 이하 → 즉시 예외.
- `MinuteAnalyzer` 생성 실패 → 즉시 예외.
- fallback은 `--allow-config-fallback` / `--allow-simple-fallback`
  을 명시할 때만.
- 리포트에 `analyzer_mode = LIVE_MINUTE_ANALYZER | SIMPLE_FALLBACK`
  출력. SIMPLE_FALLBACK이면 "1K enforce 판단에 사용하지 마십시오"
  경고를 함께 표시.

### 4. prev_close coverage 계측 + 복원 계층

**재현 (복원 전)**:
```
total_symbol_days        1781
SAME_FILE_PRETARGET       920 (51.7%)
UNAVAILABLE               861 (48.3%)   ← 절반 가까이 통째로 제외
```

`resolve_prev_close()` 추가 — 우선순위:
1. 동일 CSV의 target_date 이전 마지막 close (`confidence=high`)
2. 직전 거래일 폴더 동일 종목의 마지막 당일 close (`medium`)
3. (미구현) signal_log `price`/`change_rate_pct` 역산 — 이상치가
   있어 무조건 쓰면 안 되므로 별도 조사 후 도입
4. 불가능하면 `UNAVAILABLE`

**복원 후 실측**:
```
prev_close_coverage_pct  85.4%
  SAME_FILE_PRETARGET          920 (51.7%)
  PREVIOUS_TRADING_DAY_FILE    601 (33.7%)
  UNAVAILABLE                  260 (14.6%)
```
GPT 추정(약 70%)보다 높습니다 — 직전 거래일에 파일이 없으면 그
이전 거래일까지 거슬러 찾기 때문. **추정값을 조용히 쓰지 않고
source·confidence를 항상 기록**합니다.

### 리포트에 데이터 품질 노출

`build_quality_report()` — replay 결과에 항상 첨부:
```
── 리플레이 데이터 품질 ──
  analyzer_mode = LIVE_MINUTE_ANALYZER
  minute_bar_count = 60 (live와 동일)

  total_symbol_days        13
  prev_close_available     7
  prev_close_missing       6
  prev_close_coverage_pct  53.8%
    SAME_FILE_PRETARGET            4 (30.8%)
    PREVIOUS_TRADING_DAY_FILE      3 (23.1%)
    UNAVAILABLE                    6 (46.2%)
  ※ prev_close를 복원하지 못한 종목-일은 제외됩니다.
    따라서 이 결과는 '전체 데이터'가 아닙니다.

  horizon 품질 (target 대비 실제 경과 시간)
     5분: 표본   252  exact   251  ≤1m stale   251  1~3m stale     0  N/A     1
          실제경과 median 5.0분  p10 5.0  p90 5.0
    20분: 표본   252  exact   234  ≤1m stale   236  1~3m stale     4  N/A    12
          실제경과 median 20.0분  p10 20.0  p90 20.0
```
"5분 결과"가 실제 몇 분 가격으로 구성됐는지 드러납니다.

### 5~7. 한계 명시 (1J.5 범위 조정)

지적대로 현재 `replay_runner`는 **pattern replay이지 full live BUY
replay가 아닙니다**. `min_trading_value=0`, `v_min_bar_amount=0`이고
조건검색 universe·daily entry limit·consecutive loss·cooldown·
보유 상태·score decision·condition source reliability를 재현하지
않습니다. 따라서 1J.5는 두 단계로 분리합니다:

- **A. Aligned Value Fidelity** — 8/7 `signal_log`/
  `entry_quality_shadow`의 `(symbol, latest_bar_timestamp)`를 기준
  으로 같은 60봉을 넣어 계산값(patterns/VWAP/change_rate/pullback/
  rebound/V·PR/MACD/rolling VWAP gate)이 일치하는지. **높은 일치율
  요구 가능.**
- **B. Candidate Discovery Fidelity** — 하루 전체 스캔 비교.
  `prev_close coverage` / `minute data coverage` /
  `condition universe coverage`를 반드시 함께 표기. **"Full Live
  BUY fidelity"라고 부르지 않습니다.**

**거래대금 한계**: 현재 minute CSV에는 `acc_trde_qty`가 없고
`volume`만 있습니다. live `MinuteAnalyzer`는 `bars[-1].acc_volume`을
쓰지만 replay는 파일의 volume을 누적해 구성하므로, 장중부터 저장된
파일은 오전 거래량이 빠져 live와 다릅니다. 현재는
`min_trading_value=0`이라 영향이 숨겨져 있음. 1J.5에서 거래대금
full fidelity를 주장하지 않고, live `signal_log`의 `bar_amount`와
비교 가능한 경우만 별도 검증합니다.

### 테스트

`test_replay_time_axis.py` 67건 → **98건**:
- I절(4건) pullback MFE/MAE clock-time — 21분 봉이 실제로 제외되는지.
- J절(11건) fail-closed — 설정 로딩 실패·`minute_bar_count=0`·
  analyzer import 차단 시 각각 `ReplayConfigError`, 옵션 지정 시에만
  fallback, `analyzer_mode` 출력, silent 60 대입 부재.
- K절(16건) prev_close 복원 계층 + coverage·horizon 품질 지표 출력.

**전체 회귀**: 15개 파일 전부 통과, 1개 스킵. `compileall` 정상.

---

## 1J.3.2: prev_close 오염 제거 + replay 인프라 마무리 (2026-08-07)

1J.3.1에 대한 GPT 코드리뷰 반영. 매매 로직 무변경.
**이 단계로 replay 인프라 작업을 종료하고 1J.5로 넘어갑니다.**

### ⚠️ P0-1 정정: coverage 85.4%는 오염된 수치였음 (재현 확인)

1J.3.1의 2순위는 파일을 찾을 때까지 **과거를 무제한으로 거슬러**
올라갔습니다. 실측한 "데이터 날짜 홉" 분포:

```
 1홉 전  322건   ← 유일하게 허용돼야 함
 2홉 전   97건 / 3홉 53 / 4홉 28 / 5홉 18 / ...
36홉 전    1건
합계 오염 280건 (15.7%p)
```
**최대 36 데이터 날짜 전 종가를 전일 종가로 사용**하고 있었습니다.
등락률·A조건이 완전히 틀어지는 오염입니다.

**수정**: 2순위는 `prior[-1]`, 즉 **바로 직전 데이터 날짜 딱 1곳만**
확인합니다. 거기에 종목 파일이 없으면 `UNAVAILABLE`.

**재산출 결과**:
```
                        1J.3.1(오염)   1J.3.2(정정)
same_file_count              920           920
previous_data_day_count      601           321
unavailable_count            260           540
coverage_pct               85.4%         69.7%
calendar_gap 분포: {1일:266, 2일:1, 3일:46, 4일:8}
```
**틀린 데이터로 85%를 채우는 것보다 정확한 70%가 낫습니다.**
gap 3일 46건은 금→월 주말이라 정상이며, gap만으로 오류 판정하지
않고 기록만 합니다.

`PREVIOUS_TRADING_DAY_FILE` → **`PREVIOUS_DATA_DAY_FILE`**로 개명.
실제 거래소 캘린더를 쓰지 않으므로 "trading day"라고 단정할 수
없습니다. `previous_data_date`, `calendar_gap_days`도 함께 기록.

### P0-2: analyzer 평가 예외의 silent continue 제거

analyzer **생성**은 1J.3.1에서 fail-closed로 바꿨지만, **평가 중**
예외는 여전히 `except Exception: continue`로 후보를 조용히
삭제했습니다. 1J.5에서 live에는 있는 후보가 replay에서 사라지면
**"전략 불일치"로 오해**하게 되는 위험한 형태입니다.

`ReplayEvaluationError` 신규 — symbol / target_date / cntr_tm /
window_len / prev_close / 원래 예외를 메시지에 포함해 즉시 중단.
`--skip-analyzer-errors`를 명시할 때만 건너뛰며, 그 경우
`analyzer_error_count` / `analyzer_error_symbols` /
`analyzer_error_timestamps`를 품질 리포트에 출력하고
"1J.5/1K 기본 실행에서는 쓰지 마십시오" 경고를 붙입니다.

### P1-3: horizon bucket 중복 집계 수정

`exact`가 `≤1m stale`에도 다시 포함돼 "exact 251 / ≤1m stale 251"
처럼 보였습니다(stale이 251건이라는 뜻이 아니라 **같은 표본을 두 번
센 것**). 상호 배타적으로 분리:
```
exact      : |m - v| < eps
≤1m stale  : 0 < (m - v) <= 1
1~3m stale : 1 < (m - v) <= 3
N/A        : 별도
```
`exact + ≤1m + 1~3m + 기타 + N/A == 표본` invariant를
`build_quality_report()` 내부 `assert`로 강제하고 리포트에도 명시.
실측 예(6/23, 20분): exact 137 / ≤1m 1 / 1~3m 2 / N/A 5 = 145.

### P1-4: 품질 통계 객체화

`PREV_CLOSE_STATS`/`HORIZON_STATS` 전역 dict라 같은 프로세스에서
51거래일을 돌리면 통계가 계속 누적됐습니다. `ReplayQualityStats`
dataclass로 분리 — `prev_close_sources`, `calendar_gaps`,
`horizon_elapsed`, `analyzer_error_*`, `symbol_days`,
`analyzer_mode` + `merge()`. `run_replay(..., quality_stats=stats)`
로 전달해 **거래일별 / 전체 집계를 분리**할 수 있습니다.
`reset_replay_quality_stats()`도 함께 제공.

### P1-5: import 시 config I/O 제거

`MINUTE_BAR_COUNT = resolve_minute_bar_count()`가 모듈 최상위에서
실행돼, **설정이 정말 깨졌을 때 `--allow-config-fallback`을 줘도
main()에 도달하기 전에 예외**가 났습니다. 옵션이 필요한 상황에서
작동하지 않는 구조였습니다.

`MINUTE_BAR_COUNT: int | None = None`으로 두고 `main()`에서 CLI
파싱 후 resolve. `run_replay(..., minute_bar_count=...)`로 명시
전달. 1J.5가 이 모듈을 import하므로 side-effect를 줄이는 것이
중요합니다. 서브프로세스 테스트로 **설정이 깨져도 import 자체는
성공**하고 `MINUTE_BAR_COUNT`가 `None`임을 고정했습니다.

`analyze_v_drop_backtest.py` / `simulate_pullback_removal.py`도
import 시점 상수 참조에서 `resolve_minute_bar_count()` 직접 호출로
변경(회귀로 검출됨).

### P1-6: prev_close 품질 리포트

`total_symbol_days` / `same_file_count` / `previous_data_day_count` /
`unavailable_count` / `prev_close_coverage_pct` + `calendar_gap`
분포를 출력하고, **"바로 직전 데이터 날짜 1곳만 봅니다"**와
**"'전체 데이터'가 아닙니다"**를 명시합니다.

### 테스트

`test_replay_time_axis.py` 98건 → **133건**:
- L절(8건) prev_close 1홉 제한 — 8/6에 종목 없고 8/5에 있을 때
  UNAVAILABLE, 8/5 종가를 쓰지 않음, 8/6에 있으면 그 마지막 close,
  `previous_data_date`/`calendar_gap_days`/confidence 기록,
  무제한 탐색 코드 부재.
- M절(9건) analyzer fail-closed — 기본 `ReplayEvaluationError`,
  옵션 시에만 skip, 오류 집계·리포트 출력, `except Exception:
  continue` 부재.
- N절(7건) bucket 상호 배타 — exact 3 / ≤1m 2 / 1~3m 1 / N/A 2 = 8.
- O절(10건) 통계 객체화 + import side-effect — 인스턴스 격리,
  `merge()`, 설정이 깨져도 import 성공 및 `MINUTE_BAR_COUNT is None`.
- K절은 변경된 필드명·API에 맞춰 갱신.

`test_cost_model.py` **101건 유지**.

**전체 회귀**: 15개 파일 전부 통과, 1개 스킵. `compileall` 정상.
**실행 스모크**: replay / crash / v-drop / pullback 4종 정상.

### 1J.5 진입 조건 충족

- prev_close가 2개 이상 데이터 날짜를 거슬러 가지 않음 ✅
- 85.4% → 69.7% 재산출 및 정정 ✅
- analyzer runtime error가 조용히 candidate를 삭제하지 않음 ✅
- horizon bucket 합계 = total ✅
- 멀티데이 실행 시 날짜별 통계 분리 ✅
- import 자체가 settings.yaml을 읽지 않음 ✅
- fallback CLI가 실제 config 오류 상황에서도 작동 ✅

---

## 1J.3.3: History Completeness (2026-08-07)

1J.3.2에 대한 GPT 코드리뷰 반영. **이 단계로 replay 인프라 작업을
종료하고 1J.5로 넘어갑니다.** 매매 로직 무변경.

### P0: prev_close 숫자만 복원하고 60봉 history는 복원 안 됐음

1J.3.2는 `PREVIOUS_DATA_DAY_FILE`로 **prev_close 숫자만** 가져왔고
`ReplayDayContext.all_bars`는 당일 CSV 그대로였습니다. 그래서:

```
같은 09:05 시점
  live   : get_minute_bars(count=60) → 전일 오후 + 당일 ≈ 60봉
  replay : 당일 09:00~09:05          → 6봉
```
VWAP·고저가·pullback·V/PR·MA5·거래량 평균이 전부 달라집니다.
**이 상태로 1J.5를 재면 낮은 일치율의 원인이 계산식 차이인지
history 결손인지 구분할 수 없습니다.**

**`build_day_context()` 신규** — 세 경우를 구분:

| 상황 | 처리 | history_status |
|---|---|---|
| same-file에 pretarget 있음 | 그대로 | `SAME_FILE_COMPLETE` |
| 첫 봉 ≤09:02, pretarget 없음 | 직전 데이터 날짜 tail을 **필요한 만큼만** prepend | `PREVIOUS_DAY_RECONSTRUCTED` |
| 첫 봉이 장중(예: 10:43) | **아무것도 채우지 않음** | `INCOMPLETE_INTRADAY` |

장중 시작 파일에 전일 오후 봉을 붙이지 않는 것이 핵심입니다 —
10:43 시점 live의 최근 60봉은 당일 09:44~10:43에 가까우므로,
전일 봉으로 대체하면 **또 다른 오염**이 됩니다. `candidate`는 어느
경우에도 target_date 봉으로만 한정하고, prepend는
`minute_bar_count`를 넘지 않습니다.

**51거래일 재산출**:
```
SAME_FILE_COMPLETE             920 (51.7%)
PREVIOUS_DAY_RECONSTRUCTED      11 (0.6%)
INCOMPLETE_INTRADAY            850 (47.7%)

total_candidate_points        174,454
full_window_candidate_count   107,443
partial_window_candidate_count 67,011
full_window_coverage_pct      61.6%
```
복원 대상(장초 시작 + pretarget 없음)이 11건뿐인 이유는, 실제로는
대부분의 파일이 **장중부터 저장**되기 시작했기 때문입니다. 즉
history 결손의 주원인은 "전일 봉이 없어서"가 아니라 "그날 봇이
장중에 뜨거나 종목이 장중에 편입돼서"입니다. 이 47.7%는 **원리적으로
복원 불가**이며 억지로 채우지 않습니다.

**`full_window_coverage_pct 61.6%`가 1J.5의 핵심 지표입니다.**
Aligned Value Fidelity는 full-window 표본을 주 평가 대상으로 삼고,
partial 표본은 별도 limitation으로 출력합니다. 결과 행에도
`full_window` / `history_status`를 기록합니다.

### P1: minute_bar_count를 stats에 기록

`build_quality_report()`가 전역 `MINUTE_BAR_COUNT`를 출력해서,
1J.5처럼 `run_replay(..., minute_bar_count=60)`으로 import 실행하면
전역은 `None`이라 리포트에 `minute_bar_count = None`이 찍혔습니다.
`ReplayQualityStats.minute_bar_count`에 기록하고 리포트가 이를
출력합니다.

`merge()`에서 서로 다른 `minute_bar_count` 또는 `analyzer_mode`가
섞이면 **`MIXED_CONFIG`**로 표시하고 "1K에서 사용 금지" 경고를
붙입니다.

### P1: pullback도 동일 정책으로 통일

`simulate_pullback_removal.py`가 아직 `ctx.previous_close`만 쓰고
`except Exception: continue`가 남아 있었습니다. 즉 "네 분석기가 같은
replay infrastructure를 사용한다"는 표현이 시간축에는 맞았지만
prev_close·analyzer-error 정책까지는 아니었습니다.
`build_day_context()` + `resolve_prev_close()` + `ReplayEvaluationError`
로 통일하고 `--skip-analyzer-errors` + `ANALYZER_ERRORS` 집계 추가.

history prepend로 인덱스 기준이 `ctx.all_bars`로 바뀌면서
`current = bars[i]`가 `IndexError`를 냈고, 실행 스모크에서 검출해
수정했습니다.

### 테스트

`test_replay_time_axis.py` 133건 → **161건**:
- P절(22건) history 완전성 — GPT 지정 fixture 4종:
  (1) 전일 60봉 별도 파일 + 당일 09:00 시작 → window 60봉 구성,
      현재봉이 마지막, candidate는 당일만, prev_close 9059;
  (2) 첫 봉 10:43 → `INCOMPLETE_INTRADAY`, 전일 봉 미포함,
      full-window 아님;
  (3) same-file pretarget 존재 → `SAME_FILE_COMPLETE`, 중복 prepend
      없음;
  (4) programmatic 실행 → 리포트에 `minute_bar_count = 60`,
      전역은 `None` 유지.
  + `MIXED_CONFIG` 감지 및 경고.
- Q절(6건) pullback 정책 통일 검증.

`test_cost_model.py` **101건 유지**.

**전체 회귀**: 15개 파일 전부 통과, 1개 스킵. `compileall` 정상.
**실행 스모크**: replay / crash / v-drop / pullback 4종 정상.

### 1J.5 진입 조건 충족

- history가 없는 장초는 안전하게 복원 ✅
- history가 없는 장중은 억지 복원하지 않음 ✅
- full-window coverage를 수치로 확보 (61.6%) ✅
- 1J.5가 partial history 때문에 낮은 fidelity를 계산하지 않음 ✅
- pullback도 같은 prev_close/analyzer-error 정책 ✅

---

## 1J.3.4: prev_close 신뢰성 확정 (2026-08-07)

1J.3.3에 대한 GPT 코드리뷰 반영. **replay 인프라 작업은 이 단계로
종료하고 1J.5로 넘어갑니다.** 매매 로직 무변경.

### P0: "직전 날짜 파일의 마지막 봉"은 전일 종가가 아님 (재현 확인)

`MinuteBarSaver`는 그 종목을 감시할 때 받은 60봉만 병합 저장하므로,
전날 오전에 잠깐 감시했다면 파일도 오전에 끝납니다.

**실측 (PREVIOUS_DATA_DAY 후보 321건의 이전 파일 마지막 봉)**:
```
12:00 이전    154건
12:01~14:00    47건
14:01~14:59    31건
15:00~15:14    19건
15:15 이후     70건
→ 251건(78%)이 15:15 이전
예: 5/29 파일 마지막 09:03 → 6/1 전일 종가로 사용되고 있었음
```
반면 **same-file pretarget**(당일 API가 받아 저장한 전일 봉)의 마지막
시각은 `15:35` 868건 / `15:30` 46건으로 장 마감부까지 이어집니다.
→ `EOD_CUTOFF_HHMM = "1530"`으로 상수화.

**수정**:
- `PREVIOUS_DATA_DAY_FILE` → **`PREVIOUS_DATA_DAY_EOD`**(15:30 이후
  종료, fallback 허용) / **`PREVIOUS_DATA_DAY_PARTIAL`**(그 이전 종료,
  **prev_close 사용 금지**)로 분리.
- `PARTIAL`은 coverage의 `available`에서 제외.

### P0: history prepend에도 동일 검증

`PREVIOUS_DAY_RECONSTRUCTED`가 이전 파일이 장중까지만 있어도 tail을
붙이고 있었습니다. 7/30 파일이 10:08에 끝났는데 그 tail을 7/31
09:01의 live history로 붙이면, live가 실제로 본 7/30 장 마감 직전
60봉과 전혀 다른 데이터가 됩니다.

reconstruction 조건을 **전부 충족**할 때만 허용:
첫 target 봉이 장초 / 직전 데이터 날짜 파일 존재 / **EOD까지 존재** /
tail이 충분. 하나라도 실패하면 `INCOMPLETE_PREVIOUS_DAY`(신규) 또는
`INCOMPLETE_INTRADAY`.

### P0: SIGNAL_LOG_INFERRED 도입 — 실측으로 검증

`prev_close ≈ price / (1 + change_rate_pct / 100)`. live는 minute CSV가
아니라 시세 API의 `market_price.previous_close`를 쓰므로, 잘 검증된
역산값이 장중에 끝난 전일 파일보다 실제에 가깝습니다.

**엄격한 일관성 조건**: 유효 행 5개 이상 AND
`(max-min)/median <= 0.2%`. 미달이면 `UNAVAILABLE`.
`n_rows`·`spread_pct`를 함께 기록합니다.

**정확도 검증** (same-file 실제값이 있는 표본과 비교):
```
비교 가능 138건
절대오차 <=0.2%   138건 (100%)
절대오차 <=0.5%   138건
중앙 절대오차     0.0000%
```

**우선순위**: `SAME_FILE_PRETARGET`(high) → `SIGNAL_LOG_INFERRED`(high)
→ `PREVIOUS_DATA_DAY_EOD`(medium) → `UNAVAILABLE`.

### P1: prev_close provenance 보존

prepend한 봉이 `ctx.all_bars`에 들어가면 `resolve_prev_close(ctx)`를
다시 불렀을 때 `PREVIOUS_DATA_DAY_EOD / medium`이
**`SAME_FILE_PRETARGET / high`로 둔갑**했습니다. 수익률 숫자는 그대로
지만 1J.5에서 source별 fidelity를 볼 때 잘못된 결론이 납니다.

`ReplayContextBuildResult(ctx, history_status, prev_close)`를 반환해
**build 시점에 확정한 provenance를 다시 추론하지 않습니다.**
`__iter__`로 기존 `(ctx, status)` 언패킹 하위호환 유지. 결과 행에도
`prev_close_source` / `prev_close_confidence` 기록.
테스트 R-10이 "재추론하면 둔갑한다"를 대조군으로 고정합니다.

### P1: `SAME_FILE_COMPLETE` → `SAME_FILE_HISTORY_PRESENT`

pretarget이 1봉만 있어도 COMPLETE라 명칭이 과했습니다. 실제 full
여부는 `is_full_window()`가 판정하며, fidelity eligibility는 계속
`full_window=True` 기준입니다.

### 51거래일 재산출

```
                              1J.3.3      1J.3.4
SAME_FILE_PRETARGET            920         920 (51.7%)
SIGNAL_LOG_INFERRED              0         116 ( 6.5%)
PREVIOUS_DATA_DAY_EOD          321           5 ( 0.3%)
PREVIOUS_DATA_DAY_PARTIAL        -         260 (14.6%)  ← 사용 금지
UNAVAILABLE                    540         480 (27.0%)
신뢰 가능 coverage            69.7%       58.5%

history 완전성
SAME_FILE_HISTORY_PRESENT      920 (51.7%)
PREVIOUS_DAY_RECONSTRUCTED       4 ( 0.2%)
INCOMPLETE_PREVIOUS_DAY          7 ( 0.4%)
INCOMPLETE_INTRADAY            850 (47.7%)
```
**69.7% → 58.5%로 또 낮아졌지만 이번에도 낮아진 쪽이 정확합니다.**
이전 321건 중 316건이 실제로는 장중에 끝난 파일이었습니다.
"coverage 숫자를 높이는 것보다 previous close가 실제 previous close
인지가 우선"이라는 판단에 따릅니다.

### 1J.5 Primary Fidelity Set 정의

A(Aligned Value Fidelity)의 주 평가 표본:
```
full_window == True
AND prev_close_source in (SAME_FILE_PRETARGET, SIGNAL_LOG_INFERRED,
                          PREVIOUS_DATA_DAY_EOD)
AND analyzer_mode == LIVE_MINUTE_ANALYZER
AND analyzer_error_count == 0
```
그 외는 Secondary/limitation으로 별도 출력합니다.

### 테스트

`test_replay_time_axis.py` 161건 → **185건**:
- R절(22건) GPT 지정 fixture A~H 전부 — 이전 파일 10:30 종료 시
  fallback 금지, 15:35 종료 시 허용, 09:00 시작 + 이전 10:30 종료 시
  prepend 금지, EOD tail 충분 시 prepend 후 window 60,
  RECONSTRUCTED 후에도 source 유지(+재추론 시 둔갑하는 대조군),
  역산 10행 spread 0.05% 허용 / 1% 거부 / 3행 거부, 우선순위
  (역산 > PARTIAL), 명칭 정리, 리포트 필드.
- 기존 L·P절 fixture를 EOD 정책에 맞춰 갱신 — **갱신 전 8건이
  실패했는데, 이는 새 정책이 의도대로 작동한다는 증거**입니다.

`test_cost_model.py` **101건 유지**.

**전체 회귀**: 15개 파일 전부 통과, 1개 스킵. `compileall` 정상.
**실행 스모크**: replay / crash / v-drop / pullback 4종 정상.

---

## 1J.5 / 1J.5.1: Live ↔ Replay Fidelity 측정 (2026-08-07)

**이 단계는 replay를 고치는 단계가 아니라 "live가 실제로 계산한 값을
현재 replay가 얼마나 정확히 재현하는가"를 측정하는 단계입니다.**
매매 로직 무변경.

### 핵심 결과 — eligible recall 100%

```
Live unique candidates      67
Replay reproduced           52
Data-ineligible             15
Unexplained mismatch         0

overall recall           52/67 (77.6%)
eligible recall          52/52 (100.0%)   ← 핵심 지표
precision (replay→live)  52/991 (5.2%)
```
`77.6%`만 보면 불안하지만, **미재현 15건은 전부
`PREV_CLOSE_UNAVAILABLE`**이고 종목별로 `119850` 12건 / `233740` 3건에
몰려 있습니다. 즉 **신뢰 가능한 prev_close를 확보할 수 있었던 live
후보는 replay가 전부 다시 찾아냈고, 설명되지 않는 replay 결함은
0건**입니다.

미재현 reason_code 분포:
```
PREV_CLOSE_UNAVAILABLE   15
BAR_NOT_FOUND             0
PARTIAL_HISTORY           0
VALUE_MISMATCH            0
UNKNOWN                   0
```

### A. Aligned Value Fidelity

```
live 평가 5,281건 → replay 정렬 5,256건 (99.5%)
Primary Set 1,185건 (full_window + Tier A + LIVE analyzer + error 0)
```

**패턴 플래그** — 합격 기준(≥95%) 충족:
```
V         1185/1185  100.0%
PR        1171/1185   98.8%
패턴 조합  1148/1185   96.9%
```

**수치 항목** — 오차 분포:
| 항목 | 중앙오차 | p90 | ≤0.1 | ≤0.25 | ≤0.5 |
|---|---|---|---|---|---|
| 가격 | 0.055% | 0.196% | 65.2% | 95.6% | 99.0% |
| 등락률 | 0.060%p | 0.208 | 65.2% | 94.6% | 98.3% |
| VWAP 거리 | 0.058%p | 0.198 | 67.7% | 94.9% | 99.0% |
| 상승여력 | 0.060%p | 0.200 | 66.0% | 95.6% | 99.3% |

처음엔 "가격 일치율 35%"로 나왔는데, 원인은 **live가 결정 시점의
실시간 체결가로 판단하고 replay는 분봉 종가를 쓰기 때문**이었습니다.
형성 중인 봉에서는 구조적으로 다르며 replay 버그가 아닙니다
(`LIVE_TICK_VS_BAR_CLOSE`). 네 항목이 거의 동일한 분포를 보이는 것도
전부 현재가에서 파생되기 때문입니다.

**full-window 비율** (가드레일 2 — evaluation-point coverage 61.6%와
다른 개념):
```
aligned_live_rows        5128/5256 (97.6%)
legacy_buy_candidate      250/250 (100.0%)
accepted_buy                3/3   (100.0%)
```

### 🔴 1J.5.1 수정: `actual_buy 250/250`은 오집계였음

`signal == "BUY"`는 **전략이 BUY를 반환한 legacy candidate**이지 실제
체결이 아닙니다. 8/7 실측으로 세 소스가 일치함을 확인:
```
signal=BUY            250행
legacy_buy_candidate  250행
final_decision=BUY      3행
shadow order_accepted   3건  (005930 09:04 / 233740 10:56 / 119850 13:01)
trades.csv BUY          3건
```
`final_decision == "BUY"` 기준으로 정정하고 명칭도
`accepted_buy_full_window_pct`로 변경.

**실제 accepted BUY 재현**:
```
Accepted BUY                 3
Full-window                  3/3
Replay candidate reproduced  1/3   (005930)
Data-ineligible(prev_close)  2/3   (233740, 119850)
Unexplained mismatch         0/3
```
`3/3`으로 만들려고 prev_close 조건을 느슨하게 하지 않았습니다.
**1/3 재현 + 2/3 평가불가가 정직한 결과**입니다.

### A-2. Daily Indicator (MACD) Fidelity — 1K에서 제외 필요

live의 MACD는 `MinuteAnalyzer`가 아니라 `TradingService`가
`cached_daily_bars`(일봉 API 응답)로 계산합니다:
```
trading_service.py:1050  closes = [bar.close_price for bar in bars]
trading_service.py:1056  regime_classifier._calc_macd(closes, ...)
```
그런데 **이 일봉은 메모리 캐시일 뿐 파일로 저장되지 않습니다**
(`data/`에는 `minute_bars`만 존재). 따라서 과거 시점의 MACD를 재현할
원본 자료가 없습니다 → `MACD_DAILY_BARS_NOT_PERSISTED`.

**1K에서 MACD hard gate / MACD dead+score5 평가는 제외해야 합니다.**
검증되지 않은 새 계산 경로를 51일에 바로 적용하면 안 됩니다.
대안 (1) live `signal_log`에 이미 기록된 `macd` 값을 ground truth로
쓰는 Live-Aligned Episode 방식이면 재계산 없이 평가 가능,
(2) 일봉을 파일로 저장하는 수집기를 추가한 뒤 재검증.

### B. Candidate Discovery Fidelity

precision 5.2%는 예상된 결과입니다 — replay는
`min_trading_value=0`이고 조건검색 universe·cooldown·보유 상태·
daily risk limit을 재현하지 않아 후보를 991개 만듭니다. **낮은
precision을 replay 결함으로 판단하면 안 됩니다.**

### 가드레일 3건 반영

1. `signal_rows`를 `(거래일, 종목)` 단위로 인덱싱하고 전달 전
   `assert`로 혼입 차단. `replay_runner.main()`이 signal_log를 읽지
   않으므로 fidelity 도구가 직접 로드해 전달.
2. full-window 용어를 `aligned` / `legacy_buy_candidate` /
   `accepted_buy`로 분리.
3. prev_close 계층화 — Tier A(`SAME_FILE_PRETARGET`,
   `SIGNAL_LOG_INFERRED`) / Tier B(`PREVIOUS_DATA_DAY_EOD`) /
   Excluded(`PARTIAL`, `UNAVAILABLE`).

### 테스트

**accepted BUY 3소스 교차검증 추가**: `final_decision` 하나만 믿으면
그 필드의 의미가 바뀌었을 때 조용히 틀립니다(`signal == "BUY"`
오집계가 정확히 그런 사례). `entry_quality_shadow`
(`order_attempted AND order_accepted`)와 `trades.csv`(side=BUY)를
함께 세고, 세 값이 다르면 리포트에 경고를 출력합니다.

`test_replay_fidelity.py` 신규 **47건**. 기존 회귀는 매매·replay
로직만 보므로 **분석기 자체의 집계 오류는 잡지 못합니다** — 실제로
`250/250` 버그가 15/15를 통과한 채 리포트에 실렸습니다.
`final_decision` 기준 검증, recall/eligible recall/unexplained 산출,
accepted BUY 분류(억지로 2/2로 만들지 않는지), reason_code 상수,
Tier 정의, 가드레일 1·2, MACD limitation, 체결가/종가 한계.

**전체 회귀**: 16개 파일 전부 통과, 1개 스킵. `compileall` 정상.

### 1K 방향 조정 제안

replay가 하루 전체를 독립 발굴하는 방식(precision 5.2%)보다,
**과거 `signal_log`의 `legacy_buy_candidate=True` 시점을 ground
truth로 쓰는 Live-Aligned Episode**가 더 강합니다. 조건검색
universe·cooldown 등 live가 이미 통과시킨 표본이므로 "기존 live BUY
후보 중 어떤 gate가 나쁜 후보만 제거했는가"를 직접 볼 수 있고,
MACD도 기록된 값을 그대로 쓸 수 있습니다.
- Primary: 실제 live candidate episode
- Secondary: replay-discovered episode (limitation 병기)

---

## 1J.5.2: fidelity 분석기 의미 정정 (2026-08-07)

1J.5.1에 대한 GPT 코드리뷰 반영. **fidelity 분석기는 이 단계로
종료하고 1K Live-Aligned Episode Gate Study로 넘어갑니다.**
매매 로직 무변경.

### P0-1: `final_decision == "BUY"`는 accepted BUY가 아님

운영 코드가 명시합니다 — `trading_service.py:2515`:
```
final_decision="BUY"라도 result.accepted가 False일 수 있음
```
broker가 거절해도 `final_decision`은 `BLOCKED`로 바뀌지 않습니다.
게다가 `trades.csv` 교차검증도 `side == BUY`만 세서 **거절된 주문까지
accepted로 집계**하고 있었습니다.

**수정** — `collect_accepted_buy()` 순수 helper:
- 1순위: `entry_quality_shadow`의
  `order_attempted AND order_accepted` (order_id 있으면 unique)
- 교차: `trades.csv`의 `side=BUY AND accepted=True`
  (side=BUY 전체·거절 건수도 함께 표시)
- `final_decision=BUY`는 `BUY_DECISION` 참고값으로만 표기

8/7 결과는 세 소스가 모두 3건이라 숫자는 그대로:
```
Accepted BUY (shadow 기준)   3
  source: entry_quality_shadow order_attempted AND order_accepted [OK]
교차검증 trades accepted=True 3  (side=BUY 전체 3, 거절 0) [OK]
참고 BUY_DECISION(final_decision=BUY) 3  ← broker accepted 여부가 아님
```

### P0-2: recall 분모가 후보를 구조적으로 누락

`live_cand`를 `aligned`에서 만들어서, `NO_MINUTE_DATA` /
`BAR_NOT_FOUND`로 정렬조차 안 된 candidate가 **분모에서 통째로
사라졌습니다**. 극단적으로 "실제 후보 100개 중 30개가 minute data
없음"이면 `70/70 = 100%`로 보고됩니다.

**수정** — 분모를 **raw signal_log의 candidate 전체**로.
`classify_candidate_fidelity()`가 `REPRODUCED` /
`PREV_CLOSE_UNAVAILABLE` / `NO_MINUTE_DATA` / `BAR_NOT_FOUND` /
`PARTIAL_HISTORY` / `VALUE_MISMATCH` / `UNKNOWN`으로 100% 분류하고,
`calculate_recall()`이 overall/eligible을 계산합니다.

8/7은 제외 25행 중 candidate가 0건이라 결과가 그대로 재현됩니다:
```
Live unique candidates   67  (raw signal_log 기준)
Replay reproduced        52
Data-ineligible          15
Unexplained mismatch      0
overall recall        52/67 (77.6%)
eligible recall       52/52 (100.0%)
```

### P1: 교차검증 입력을 명시적으로

`--signal-log`만 지정 가능하고 shadow/trades는 `logs/`에서
하드코딩으로 읽어서, **bundle signal log와 서버 logs를 섞어 비교**할
수 있었습니다(1J.5.1 리포트의 `3/0/0`이 정확히 그 사례).

`--entry-quality-shadow` / `--trades` CLI 추가. 미지정 시
signal-log와 같은 폴더의 sibling을 자동 탐색하며, **실제 사용 경로를
리포트 상단에 항상 출력**합니다. 상태를 `OK` /`FILE_MISSING` /
`NO_TARGET_DATE_DATA` / `PARSE_ERROR`로 구분 — **"실제 0건"과
"그 날짜 자료 없음"은 다릅니다.** 파싱 예외도 silent pass하지 않고
상태·에러를 남깁니다.

### P1: 테스트를 실행 기반으로

1J.5.1의 47건은 분석기 로직을 테스트 쪽에 다시 구현해서
**"final_decision == accepted BUY"라는 잘못된 가정을 코드와 함께
공유**했고, 그래서 47/47이 통과했습니다.

순수 helper(`collect_accepted_buy` / `classify_candidate_fidelity` /
`calculate_recall` / `_read_day_rows`)를 **실제 호출**하는 fixture로
전환. 기존 invariant는 삭제하지 않고 유지했습니다.

핵심 fixture 2종:
- broker rejected BUY(`shadow accepted=False`, `trades accepted=False`)
  → **accepted BUY 0** (1J.5.1 구현은 이걸 통과하지 못했습니다)
- live candidate 3건 중 1건 `NO_MINUTE_DATA`
  → overall 2/3, data-ineligible 1, **eligible 2/2**

### P1: MACD 정책 정정 — 1K에서 완전 제외하지 않음

replay로 MACD를 **재계산**할 수 없다는 한계는 그대로입니다(일봉
미저장). 그러나 1K Primary가 Live-Aligned Episode이므로 **live가
실제로 쓴 값이 signal_log에 이미 저장돼 있어 재계산이 불필요**합니다.

8/7 coverage 실측:
```
macd                전체 5196/5256   candidate 250/250
macd_signal         전체 5196/5256   candidate 250/250
macd_above_signal   전체 5196/5256   candidate 250/250
macd_hist_direction 전체 5196/5256   candidate 250/250
score               전체 1330/5256   candidate 250/250
```

```
Primary (Live-Aligned)       → MACD 평가 가능
  A) hard gate     : macd_above_signal == False → block
  B) dead + score5 : macd_above_signal == False AND score < 5 → block
Secondary (Replay Discovery) → MACD 평가 불가 (일봉 없음)
```
coverage가 없는 표본은 N/A 처리.

### 테스트

`test_replay_fidelity.py` 47건 → **78건** (10절 실행 기반 27건 +
MACD 정책 정정 반영). **전체 회귀 16개 파일 통과**, 1개 스킵.
`compileall` 정상.

**8/7 핵심 결과 재현**: eligible recall 52/52 = 100%,
accepted BUY 3(shadow/trades/decision 3/3/3), unexplained 0.

---

## 1J.5.3: fidelity 분석기 집계 정확도 마무리 (2026-08-07)

1J.5.2에 대한 GPT 코드리뷰 반영. **fidelity 분석기는 이 단계로
완전히 종료하고 1K Live-Aligned Episode Gate Study로 넘어갑니다.**
매매 로직 무변경.

### 1. accepted BUY의 order_id dedupe가 반대로 작동 (재현 확인)

```python
shadow_n = len(ids) if len(ids) == len(acc) and ids else len(acc)
```
이 조건은 **중복 order_id가 있을 때 오히려 dedupe를 건너뜁니다**
(`len(ids) != len(acc)`가 되므로 `else` 분기로 감).

**재현**: 같은 `order_id=O1` accepted 2행
→ `accepted_count=2`, `accepted_keys=[('A','T1'), ('A','T1')]`.
실제로는 주문 1건입니다. trades도 동일.

**수정** — `_dedupe()` 내부 함수:
- order_id가 **전부 있으면** order_id 기준 unique
- **하나라도 비어 있으면** 조용히 row count로 fallback하지 않고
  `shadow_order_id_missing` / `trades_order_id_missing`을 True로 두고
  리포트에 `⚠ ORDER_ID_MISSING` 경고 출력
- `accepted_keys`도 unique 기준으로 반환

### 2. `BAR_NOT_FOUND`가 죽은 코드였음 (재현 확인)

`classify_candidate_fidelity()`가 `a.get("_bar_not_found")`를 보는데
**그 키를 설정하는 코드가 프로젝트 어디에도 없었습니다**
(등장 1회 = 읽기만, 쓰기 0회). 따라서 분봉 CSV는 있는데 해당
`latest_bar_timestamp` 봉만 없는 candidate까지 전부
`NO_MINUTE_DATA`로 오분류됐습니다.

**수정** — alignment 단계에서 `alignment_status[(symbol, lbt)]`에
`ALIGNED` / `NO_MINUTE_DATA` / `BAR_NOT_FOUND`를 보존하고
`classify_candidate_fidelity(..., alignment_status)`로 전달.
상태 map이 없으면 보수적으로 `NO_MINUTE_DATA`로 처리합니다.
죽은 `_bar_not_found` 참조는 제거.

### 3. 테스트에 1J.5.1의 잘못된 가정이 남아 있었음

가장 아이러니한 부분입니다. **1J.5.2가 "final_decision ≠ accepted
BUY"를 고친 버전인데, 테스트 앞부분은 여전히 옛 가정을 검증**하고
있었습니다(`"actual_buy가 final_decision 기준으로 집계됨"`).

두 개념을 분리해 재작성:
```
BUY_DECISION = final_decision == "BUY"      (broker accepted 아님)
ACCEPTED_BUY = collect_accepted_buy(...)     (shadow/trades 기준)
```
fixture로 `final_decision=BUY` 1건 + broker 거절 → **accepted BUY 0**을
검증합니다.

또 `"accepted_buy_full_window_pct" in src`는 **파일 상단 docstring에
그 문자열이 있어서 PASS**하던 false pass였습니다(실제 리포트 항목은
`buy_decision_full_window_pct`). AST로 docstring을 걷어낸 실행
코드에서만 검사하도록 바꾸고, "docstring에만 있는 문자열은 통과시키지
않는다"를 별도 검사(7-1b)로 고정했습니다.

### 4. sibling 자동 탐색이 날짜를 무시

`sorted(glob(...))[0]`이라 같은 폴더에
`entry_quality_shadow_20260806/07/10.csv`가 있으면 8/10 분석에도
**8/06 파일**을 골랐습니다.

**수정** — `_pick_sibling()`: (1) 파일명에 target 날짜가 들어간 것을
최우선, (2) 없으면 target-date row가 실제 존재하는 파일을 탐색,
(3) 후보가 둘 이상이면 경고 후 CLI 명시 요구.

### 8/7 핵심 결과 — 그대로 재현

```
입력 signal_log : b7/raw/signal_log_20260807.csv
입력 shadow     : b7/raw/entry_quality_shadow_20260807.csv   ← 날짜 정확 매칭
입력 trades     : b7/raw/trades_20260807.csv

Live unique candidates   67  (raw signal_log 기준)
Replay reproduced        52
Data-ineligible          15
Unexplained mismatch      0
eligible recall       52/52 (100.0%)

Accepted BUY (shadow 기준)   3
교차검증 trades accepted=True 3  (side=BUY 전체 3, 거절 0) [OK]
참고 BUY_DECISION(final_decision=BUY) 3  ← broker accepted 여부가 아님
```

### 테스트

`test_replay_fidelity.py` 78건 → **102건**:
- 1절 재작성 — BUY_DECISION / ACCEPTED_BUY 분리, broker 거절 fixture.
- 11절(17건) 중복 order_id dedupe(1건으로), 서로 다른 id는 2건 유지,
  order_id 누락 시 MISSING 표시, `NO_MINUTE_DATA`와 `BAR_NOT_FOUND`
  실제 구분, 상태 map 부재 시 보수적 처리, 죽은 코드 제거 확인.
- 12절(6건) sibling 날짜 우선 — 8/10 분석 시 8/06이 아니라 8/10을
  고르는지, 예전 방식(`sorted[0]`)이었다면 틀렸을 것도 함께 고정.
- 7-1b docstring false pass 방지.

**전체 회귀**: 16개 파일 전부 통과, 1개 스킵. `compileall` 정상.

---

## 1P0.1: 부분체결 lifecycle 정확성 (2026-08-10)

**1K 결과(게이트 무효)보다 이것이 먼저입니다.** 8/10 실서버에서
부분체결이 상태머신을 오염시켜 중복 주문·ERROR 상태를 만들었고,
그 상태로 수집한 데이터는 신뢰할 수 없기 때문입니다.

### 재현 — 8/10 실서버 3건

```
047040  BUY 353 요청
  11:40:00  broker 146  PARTIAL_FILL_PENDING
  11:41:10  broker 353  PARTIAL_FILL_PENDING   ← 전량인데도 미확정
  11:41:27  broker 353  PENDING_STILL_UNCONFIRMED
  13:34:49  SELL 353 → broker 343  PARTIAL_FILL → OPEN 전환
  13:34:50  343주 재매도 → SELL_REJECTED
            (app.log: "매도가능수량 부족" 11회)

006360  BUY 174 → broker 7 (BUY_PENDING)
  11:42:01  SELL 7 발행 → broker 167  UNEXPECTED_QUANTITY_INCREASE → ERROR

017900  BUY 670 → broker 66 (BUY_PENDING)
  12:25:37  SELL 66 발행 → broker 604 → ERROR
  13:56:57  ERROR 상태에서 604 매도 → 14:00:01 FLAT
```

### P0-1: expected 수량이 부분체결마다 함께 움직였음

```python
expected_min = state.known_quantity + state.pending_quantity
```
부분체결로 `known_quantity`가 갱신되면 목표도 같이 올라갑니다.
047040은 146 체결 후 목표가 `146+353=499`가 되어 **353주 전량이
들어와도 영원히 도달 불가**였습니다.

**수정** — 주문 시작 시점의 불변 스냅샷:
```
base_quantity_before_order   주문 직전 실제 보유수량
requested_quantity           이번 주문 요청 수량
expected_final_quantity      base + requested   ← terminal까지 불변
observed_quantity            관찰된 최신 브로커 수량
```
`known_quantity`는 관찰값 역할만 하고 **목표 계산에 다시 쓰지
않습니다**. 판정:
```
broker == expected_final          → OPEN (전량 체결)
broker >  expected_final          → ERROR (EXCESS)
base < broker < expected_final    → BUY_PENDING 유지 (부분체결)
broker <  base                    → ERROR (DECREASE, 신규)
그 외                              → PENDING_STILL_UNCONFIRMED
```

**수정 후 047040**: `146 → BUY_PENDING`, `353 → OPEN`.

### P0-2: SELL 부분체결이 OPEN으로 되돌아가 중복 매도 유발

부분체결 시 `OPEN`으로 전이하고 `pending_order_id`를 지웠습니다.
그러면 원 SELL 주문의 나머지가 살아 있는데 "새 포지션"으로 보여
잔여수량에 대한 **추가 SELL을 발행**합니다.

**수정** — `sell_base_quantity`(SELL 직전 보유수량)를 고정하고,
`0 < broker < sell_base`면 `SELL_PENDING`을 유지하며 pending order
정보도 보존합니다. **`broker == 0`일 때만 FLAT**입니다.

### P0-3 / P0-4: 주문 guard (shadow — 관측 전용)

`would_block_sell()` / `would_block_buy()` 추가:
- `SELL_PENDING` 중 추가 SELL → `WOULD_BLOCK_DUPLICATE_SELL`
- `BUY_PENDING` 중 추가 BUY → `WOULD_BLOCK_BUY_WHILE_PENDING`
- `ERROR` 상태 BUY → `WOULD_BLOCK_BUY_IN_ERROR`

**현재는 주문을 막지 않습니다.** `_try_sell()`에서 사유를
`[LIFECYCLE_SHADOW]` 경고로만 남기고 주문은 그대로 진행합니다 —
shadow 검증 없이 enforce하지 않는다는 원칙에 따릅니다. guard 호출이
상태를 변경하지 않는 것도 테스트로 고정했습니다(4-9).

### 테스트

`test_partial_fill_lifecycle.py` 신규 **51건**:
- 1절 BUY 부분체결 → 전량 체결(047040), 목표 불변 확인, 다중
  부분체결에도 목표 353 유지.
- 2절 기존 보유분(100) + 추가매수(50) → 120 부분 → 150 완결,
  초과 EXCESS, 감소 DECREASE.
- 3절 SELL 부분체결(047040), 0일 때만 FLAT, 미반영/거부/증가(006360).
- 4절 guard — 중복 SELL 감지, FLAT 후 해제, BUY_PENDING·ERROR,
  관측 전용 확인, 실매매 경로가 shadow인지.
- 5절 AST로 옛 `known + pending` 방식이 실행 코드에 없는지 고정.

**전체 회귀**: 17개 파일 전부 통과, 1개 스킵. `compileall` 정상.

### 다음

- 1P0.2 SELL 재시도 정책 (`매도가능수량 부족` 11회 → backoff)
- 1P0.3 shadow 관측 후 guard enforce 전환
- 그 다음에 Entry Timing / Regime Study

---

## 1P0.2: SELL 중복/재시도 enforce + BUY guard 실경로 적용 (2026-08-10)

1P0.1에 대한 GPT 코드리뷰 반영. **매매 실행 경로를 실제로 변경합니다.**

### 왜 shadow에서 enforce로 올렸는가

1P0.1은 lifecycle 상태만 정확히 만들고 guard는 경고만 남겼습니다.
그러나 **1P0.1을 적용해도 8/10과 같은 상황이 다시 발생합니다** —
`_try_sell` 호출 자체를 막지 않기 때문입니다. 8/10 실측:
```
13:34:50 ~ 13:37:xx  "매도가능수량이 부족합니다"  11회 (16초 간격)
```
정상 운영에서 나오면 안 되는 실패이고 브로커 rate limit 위험도
있어, shadow로 며칠 더 두는 것이 오히려 위험하다고 판단했습니다.

### P0-A: `_try_sell`을 단일 관문(dispatcher)으로

호출부마다 guard를 넣으면 새 경로가 생길 때 빠집니다. 그래서
`_try_sell`을 dispatcher로 만들고 실제 주문 발행을
`_try_sell_unchecked`로 분리했습니다. **모든 SELL 경로가 반드시
guard를 통과**합니다(호출부 2곳 무수정).

`force=True`는 하드 손절·강제청산처럼 반드시 나가야 하는 경로용
우회구입니다. guard가 잘못 잠겨 청산이 막히는 상황을 피하기 위한
안전판이며, 사용 시 `[LIFECYCLE_FORCE]`로 남깁니다. 이월 방지
강제청산 경로에 적용했습니다.

차단은 `[LIFECYCLE_BLOCK]`으로 기록합니다.

### P0-B: 재시도 backoff

브로커 거부 시 `sell_reject_count` / `sell_reject_last_at` /
`sell_reject_last_reason`을 누적하고, 지수적으로 대기합니다.
```
SELL_RETRY_BACKOFF_SECONDS = (0, 30, 60, 120, 300)
SELL_REJECT_MAX_RETRY = 5
```
체결이 진행되면 카운터를 리셋합니다.

**8/10 재현 결과**: 동일 조건 11회 시도 →
**실제 발행 1회 / 차단 10회**.

### P0-C: BUY guard를 실제 매수 경로에 적용

`_try_buy()` 최상단에서 `would_block_buy()`를 확인하고 차단 사유를
반환합니다(기존 반환 시그니처 그대로라 상위 로직이 `order_block_reason`
으로 기록). `BUY_PENDING` 중 재매수와 `ERROR` 상태 매수를 막습니다 —
8/10 006360/017900에서 부분체결 미확정 상태에 주문이 겹쳐
`UNEXPECTED_QUANTITY_INCREASE(ERROR)`가 발생한 경로입니다.

### 명칭 정리

`WOULD_BLOCK_*` → `BLOCK_*`. shadow가 아니라 실제 차단이므로
이름이 동작과 일치해야 합니다.

### 테스트

`test_partial_fill_lifecycle.py` 51건 → **69건**:
- 6절(18건) — dispatcher 구조(`_try_sell_unchecked` 호출이 내부
  1곳뿐인지 정규식 확인), `force=True` 우회, BUY guard 실경로,
  backoff 누적·해제·최대 재시도·체결 시 리셋, **8/10 11회 시나리오가
  1회 발행으로 줄어드는지**, guard 조회가 상태를 바꾸지 않는지.
- 4절은 enforce 전환에 맞춰 갱신.

**전체 회귀**: 17개 파일 전부 통과, 1개 스킵. `compileall` 정상.

### 다음 거래일 확인 사항

- `[LIFECYCLE_BLOCK]` 발생 빈도 — 과도하면 guard가 정상 매도까지
  막고 있다는 신호
- `[LIFECYCLE_FORCE]` 발생 여부 — 강제청산이 guard에 걸렸는지
- `매도가능수량 부족` 재발 여부 (0이어야 정상)
- `UNEXPECTED_QUANTITY_INCREASE` 재발 여부

---

## 1P0.3: PENDING 타임아웃 — 손절 영구 봉쇄 방지 (2026-08-10)

1P0.2에 대한 GPT 코드리뷰 반영. **1P0.2를 그대로 배포하면 안 됩니다.**

### 🔴 P0-A: 부분체결 후 잔여수량이 영구 미매도 (재현 확인)

1P0.2는 두 변경을 겹쳤는데, 조합이 치명적이었습니다.
- SELL 부분체결 시 `SELL_PENDING` 유지 (1P0.1)
- `SELL_PENDING`이면 `_try_sell` 차단 (1P0.2)

브로커가 잔여수량을 체결하지 않으면 `SELL_PENDING`이 풀리지 않고,
그동안 **손절 신호가 와도 모두 차단**됩니다.

**재현**:
```
SELL 353 → broker 343 (10주만 체결)
  폴링1~5: SELL_PENDING  known=343  → _try_sell 차단
  → 343주가 영원히 매도 불가. 손실이 무한정 커짐.
```
제가 1P0.2 보고에서 "손절 지연"을 우려했는데, 실제로는 **지연이
아니라 영구 봉쇄**였습니다.

**수정** — PENDING 타임아웃:
```
SELL_PENDING_TIMEOUT_SEC = 60     이후 잔여수량 재매도 허용
BUY_PENDING_TIMEOUT_SEC  = 120    이후 관측값으로 상태 확정
```
- `sell_pending_stale()`이 타임아웃을 넘긴 PENDING을 판별하고,
  `would_block_sell()`이 그 경우 차단하지 않습니다.
- `resolve_stale_pending()`이 관측 잔고 기준으로 상태를 확정합니다
  (`잔고>0 → OPEN`, `잔고=0 → FLAT`). `_sync_position_state_machine_shadow`
  에서 매 폴링 호출하며 `[LIFECYCLE_TIMEOUT]`으로 기록합니다.
- 기준 시각은 `partial_fill_since or pending_since` — 부분체결이
  계속 진행 중이면 그 시점부터 다시 셉니다.

미결 주문이 살아 있는데 재매도할 위험은 남지만, **손절을 영구히
막는 것보다 재시도 가능 상태가 안전**하다고 판단했습니다.

### P0-B: 손절·강제청산이 guard보다 우선

1P0.2는 `force=True`를 이월 방지 경로에만 붙였습니다. 하드 손절이
중복 SELL guard에 막히면 손실이 커집니다.

`FORCED_EXIT_KEYWORDS = ("손절", "강제청산", "이월 방지", "stop", "긴급")`
로 `exit_reason`을 판별해 **자동으로 우회**합니다. 우회 시
`[LIFECYCLE_FORCE]`로 남깁니다.
```
"손절 — 평균단가 대비 -3.0%"        → force
"이월 방지 강제청산"                 → force
"트레일링 청산"                      → guard 통과
"entry_watch 최소수익미달청산"        → guard 통과
```

### P0-C: BUY_PENDING 무기한 방지

미체결 잔량이 취소돼도 상태머신은 알 수 없어 `BUY_PENDING`이 굳고
재매수가 영구 차단됐습니다. `BUY_PENDING_TIMEOUT_SEC`(120초) 후
`resolve_stale_pending()`이 관측 잔고로 확정합니다
(`잔고>0 → OPEN`, `0 → FLAT`).

### P0-D: BUY 차단 사유 오염

`order_block_reason`에 `BLOCK_BUY_WHILE_PENDING(pending_order=b1,
expected_final=174, observed=7)`처럼 파라미터가 섞인 문자열이 그대로
들어가, **값마다 다른 사유**가 되어 skip_reason 분포·집계가
오염됐습니다.

`would_block_buy_detail()`이 `(code, detail)`을 분리 반환하고,
`order_block_reason`에는 **파라미터 없는 code만** 넣습니다. 상세는
`[LIFECYCLE_BLOCK]` 로그로만 남깁니다.

### 테스트

`test_partial_fill_lifecycle.py` 69건 → **96건**:
- 7절(15건) 타임아웃 — 부분체결 직후 차단(정상), 타임아웃 전 resolve
  무동작, 타임아웃 후 guard 해제, OPEN/FLAT 확정, BUY_PENDING
  타임아웃, 정상 체결은 무관, sync 연결·로그.
- 8절(6건) 강제 경로 — 손절/강제청산 우회, 일반 청산은 guard 통과,
  dispatcher 자동 판별.
- 9절(5건) 차단 code 안정성 — 수량이 달라도 code 동일, code에
  파라미터 없음, detail은 로그로만.
- 4·6절은 API 변경에 맞춰 갱신.

**전체 회귀**: 17개 파일 전부 통과, 1개 스킵. `compileall` 정상.

### 다음 거래일 확인 사항 (우선순위 순)

1. **`[LIFECYCLE_TIMEOUT]`** — 자주 뜨면 타임아웃이 너무 짧거나
   브로커 반영이 느린 것
2. **`[LIFECYCLE_BLOCK]` SELL** — 과도하면 정상 매도를 막고 있음
3. **`[LIFECYCLE_FORCE]`** — 손절이 guard에 걸린 횟수
4. `매도가능수량 부족` / `UNEXPECTED_QUANTITY_INCREASE` — 0이어야 정상
5. `order_block_reason` 분포에 `BLOCK_BUY_*`가 정상 집계되는지

---

## 1P0.4: Orphan order 추적 — 타임아웃 후 이중 매도 방지 (2026-08-10)

1P0.3에 대한 GPT 코드리뷰 반영.

### 🔴 P0-A: 타임아웃이 원 주문의 존재를 지웠음 (재현 확인)

1P0.3의 `resolve_stale_pending()`은 `pending_order_id`를 `None`으로
지웠습니다. 그런데 **브로커에 남아 있는 원 주문은 사라지지 않습니다.**

**재현**:
```
SELL 353 → broker 343 (부분체결)
타임아웃 후: OPEN  known=343  pending_id=None
→ 새 SELL 발행 가능: True   (원 주문 s1은 브로커에 살아있을 수 있음)
```
원 주문 + 새 주문이 동시에 체결되면 **이중 매도**입니다.

**수정** — orphan 주문으로 이관:
```
orphan_order_id        원 주문 id (잊지 않음)
orphan_since           이관 시각
orphan_expected_delta  체결 시 예상 수량 변화 (SELL 음수 / BUY 양수)
```
`has_orphan_order()`가 True인 구간에는
`BLOCK_SELL_ORPHAN_ORDER(order=s1, age=..s)`로 새 SELL을 막습니다.
**손절은 `force`로 우회하므로 이 차단이 손절을 막지는 않습니다.**

브로커가 주문 취소 API를 제공하지 않으므로 취소는 못 하고, "미확인
주문이 있다"는 사실을 보존해 그 구간의 매도를 제한하는 방식입니다.

### P0-B: sync가 PENDING 종목을 skip — 해당 없음

확인 결과 `_sync_position_state_machine_shadow`는
`set(self.targets) | set(psm._states.keys()) | set(broker_qty_by_symbol)`
를 순회하므로, **잔고에서 사라진 종목도 `broker_qty=0`으로 커버**되고
`SELL_PENDING`/`BUY_PENDING` 분기에서 매 폴링 처리됩니다. 수정
불필요합니다.

### P0-C: orphan 자동 해소

`observe_for_orphan()` — 브로커가 주문 조회 API를 주지 않으므로
**잔고 변화로만** 추정합니다.
```
SELL orphan + 잔고 감소  → 체결로 간주, 해소
BUY  orphan + 잔고 증가  → 체결로 간주, 해소
ORPHAN_TTL_SEC(600s) 경과 → 미체결로 간주, 해제
```
`_sync_position_state_machine_shadow`에서 매 폴링 호출하고
`[LIFECYCLE_ORPHAN]`으로 기록합니다.

### P1: orphan 구간 invariant 오탐 방지

orphan 주문이 살아 있으면 로컬 상태와 브로커 잔고가 어긋나는 것이
정상입니다. `check_invariant()`가 이 구간에는 `None`을 반환해
`POSITION_STATE_MISMATCH` CRITICAL 오탐을 막습니다.

### P1: 강제 매도 최소 간격

1P0.3은 `force=True`면 guard를 완전히 우회했습니다. 8/10처럼 브로커가
계속 거부하면 손절 사유로 **16초마다 영원히 재시도**하게 됩니다.

`FORCED_SELL_MIN_INTERVAL_SEC = 30`을 두고, 미달 시
`[LIFECYCLE_FORCE_THROTTLE]`로 남기고 발행하지 않습니다.
**guard에 걸리지 않은 정상 손절은 throttle 대상이 아닙니다**
(`if block and force:` 안에서만 적용) — 손절 자체가 지연되지
않습니다.

### 테스트

`test_partial_fill_lifecycle.py` 96건 → **122건**:
- 10절(21건) orphan — 타임아웃 detail의 ORPHAN 표기, 기록/차단/
  사유 형식, 잔고 감소·증가로 해소, 변화 없으면 유지, TTL 자동 해제,
  BUY orphan, invariant 오탐 방지(FLAT+잔고 fixture로 정정), sync 연결.
- 11절(5건) 강제 매도 throttle — 상수, 종목별 기록, 차단, 발행 전
  return 순서, guard 없을 때는 미적용.

**전체 회귀**: 17개 파일 전부 통과, 1개 스킵. `compileall` 정상.

### 다음 거래일 확인 사항

1. `[LIFECYCLE_ORPHAN]` — 해소 사유가 "잔고 감소/증가"인지 "TTL"인지.
   TTL이 많으면 타임아웃이 짧아 orphan을 과하게 만드는 것
2. `[LIFECYCLE_BLOCK] ... ORPHAN` — 빈도
3. `[LIFECYCLE_FORCE_THROTTLE]` — 손절이 30초 간격에 걸린 횟수
4. `[LIFECYCLE_TIMEOUT]` / `매도가능수량 부족` / `UNEXPECTED_QUANTITY_INCREASE`

---

## 1P0.5: 매도 결정 단일화 + 영구 봉쇄 안전밸브 (2026-08-10)

1P0.4에 대한 GPT 코드리뷰 반영. **1P0.1~1P0.4를 배포 가능한 상태로
수렴시키는 단계입니다.**

### 문제 — 차단 규칙 6종이 흩어져 조합 검증 불가

```
BLOCK_DUPLICATE_SELL / BLOCK_SELL_ORPHAN_ORDER / BLOCK_SELL_RETRY_BACKOFF
BLOCK_SELL_MAX_RETRY / BLOCK_BUY_WHILE_PENDING / BLOCK_BUY_IN_ERROR
+ PENDING 타임아웃 2종 + ORPHAN TTL + FORCE throttle
```
서비스 계층과 상태머신 양쪽에 판단이 나뉘어 있어, 조합에 따라
매도가 아예 나가지 않는 구간이 실제로 존재했습니다.

**재현 A — orphan 무한 차단**:
```
0s / 60s / 300s / 599s 경과 → 전부 차단
→ TTL 600초 동안 비강제 매도 전면 차단
```
**재현 B — MAX_RETRY 영구 차단**:
```
count=6, 대기 무한경과 → BLOCK_SELL_MAX_RETRY
→ 카운터가 리셋되는 유일한 경로가 '체결 성공'뿐
   그런데 매도가 차단돼 체결될 수 없음 = 영구 봉쇄
```

### 해결 1 — `decide_sell()` 단일 진입점

모든 판단을 한 함수에서 **명시적 우선순위**로 평가합니다.
```
1. 안전밸브    연속 차단 >= MAX_BLOCK_DURATION_SEC(300s) → ALLOW_ESCALATED
2. forced      손절·강제청산 → throttle만 적용하고 통과
3. 중복 SELL   SELL_PENDING (타임아웃 전)
4. orphan      미확인 원 주문
5. backoff     재시도 대기 / MAX_RETRY
6. 그 외       ALLOW
```
`SellDecision`(ALLOW / ALLOW_FORCED / ALLOW_ESCALATED / THROTTLED /
BLOCKED)과 `SellDecisionResult(code, detail)`을 반환합니다. 차단
규칙은 `_evaluate_sell_blocks()` 한 곳에만 있습니다.

서비스 계층은 판단하지 않고 **기록과 실행만** 합니다.
`_last_forced_sell_at` throttle도 상태머신으로 이관해 이중 적용을
없앴습니다.

### 해결 2 — 안전밸브 (영구 봉쇄 최후 보루)

`blocked_since` / `blocked_count` / `last_block_code`로 연속 차단을
추적하고, 300초를 넘으면 **사유 불문 매도를 허용**합니다
(`ALLOW_ESCALATED`). 사람이 반드시 확인해야 하므로
`[LIFECYCLE_ESCALATED]` **CRITICAL**로 남깁니다.

### 해결 3 — 거부 카운터 시간 감쇠

`SELL_REJECT_DECAY_SEC = 300`. 마지막 거부 후 5분이 지나면 카운터를
1씩 줄여 재시도 기회를 회복합니다. '체결 성공'만이 유일한 탈출구였던
구조를 바꿉니다.

### 차단 조합 매트릭스 (테스트로 증명)

```
상태               일반매도        강제(손절)         5분경과
clean                 ALLOW      ALLOW_FORCED           ALLOW
sell_pending        BLOCKED      ALLOW_FORCED  ALLOW_ESCALATED
orphan              BLOCKED      ALLOW_FORCED  ALLOW_ESCALATED
backoff             BLOCKED      ALLOW_FORCED  ALLOW_ESCALATED
max_retry           BLOCKED      ALLOW_FORCED  ALLOW_ESCALATED
orphan+backoff      BLOCKED      ALLOW_FORCED  ALLOW_ESCALATED
pending+orphan      BLOCKED      ALLOW_FORCED  ALLOW_ESCALATED
```
**모든 조합에서 (1) 손절은 항상 통과하고 (2) 5분 후 해제됩니다.**
영구 봉쇄가 존재하지 않음을 매트릭스로 고정했습니다(12-1, 12-2).

### 테스트

`test_partial_fill_lifecycle.py` 122건 → **140건**:
- 12절(18건) 조합 매트릭스 7종 × 3시나리오, 안전밸브 동작·추적
  리셋, MAX_RETRY 감쇠 회복, 단일 진입점 구조 검증.
- 6·11절은 새 구조에 맞춰 갱신.

**전체 회귀**: 17개 파일 전부 통과, 1개 스킵. `compileall` 정상.

### 배포 시 확인 (우선순위)

1. **`[LIFECYCLE_ESCALATED]` (CRITICAL)** — 안전밸브가 열린 것.
   자주 뜨면 차단 규칙이 과합니다.
2. `[LIFECYCLE_BLOCK]` 특정 종목 반복 — 즉시 중단 검토
3. `[LIFECYCLE_ORPHAN]` 해소 사유가 "잔고 변화"인지 "TTL"인지
4. `[LIFECYCLE_TIMEOUT]` / `[LIFECYCLE_FORCE_THROTTLE]`
5. `매도가능수량 부족` / `UNEXPECTED_QUANTITY_INCREASE` — 0이어야 정상

---

## 1P0.6: 폴링 기반 안전밸브 + ERROR 자동 회복 (2026-08-10)

1P0.5에 대한 GPT 코드리뷰 반영.

### 🔴 P0-A: 안전밸브가 SELL 신호에 의존 (재현 확인)

1P0.5의 안전밸브는 `decide_sell()` 안에만 있었습니다. `blocked_since`
갱신도 거기서만 일어납니다. 그런데 `decide_sell()`은 **SELL 신호가
있을 때만** 호출됩니다.

**재현**:
```
t=0    decide_sell 호출 → BLOCKED (blocked_since 기록)
       이후 전략이 HOLD만 반환하면 decide_sell이 호출되지 않음
t=400  (SELL 신호가 다시 왔을 때에야) → ALLOW_ESCALATED
```
그 사이 포지션은 **조용히 갇혀 있습니다.** 안전밸브가 "5분 후 열린다"
가 아니라 "5분 후 SELL 신호가 오면 열린다"였습니다.

**수정** — `check_block_escalation()`을 `_sync_position_state_machine_shadow`
에서 **매 폴링 호출**합니다. 신호와 무관하게 차단 지속을 감시하고,
`MAX_BLOCK_DURATION_SEC`를 넘기면 `[LIFECYCLE_STUCK]` **CRITICAL**로
즉시 노출합니다. 차단이 해소되면 추적도 자동 리셋합니다.

### 🔴 P0-B: ERROR 상태에서 매수 영구 차단 (재현 확인)

`BLOCK_BUY_IN_ERROR`는 `acknowledge_error()`로만 풀리는데, 이 함수는
**사람이 계좌를 확인한 뒤 호출하는 인터페이스**라 무인 운영에서는
호출되지 않습니다.

**재현**:
```
ERROR 진입 후
      0s → BLOCK_BUY_IN_ERROR
   3600s → BLOCK_BUY_IN_ERROR
  86400s → BLOCK_BUY_IN_ERROR   (하루 지나도 그대로)
```

**수정** — `auto_recover_error()`. ERROR 진입 시각(`error_since`)을
기록하고 `ERROR_AUTO_RECOVERY_SEC`(900초) 경과 시 **브로커 잔고를
신뢰해** 상태를 확정합니다(`잔고>0 → OPEN`, `0 → FLAT`).
sync 루프에서 매 폴링 호출하며 `[LIFECYCLE_ERROR_RECOVERY]`로
기록합니다. 사람 확인용 `acknowledge_error()`는 그대로 유지합니다.

### P1: 강제 매도 실패 escalation

손절·강제청산이 브로커에 거부되면 시스템이 자체 해결할 수 없습니다.
연속 실패 횟수를 종목별로 추적하고 `[FORCED_SELL_FAILED]`
**CRITICAL**로 즉시 노출합니다(성공 시 카운터 리셋).

### P1: throttle보다 안전밸브 우선

`decide_sell()`의 평가 순서상 안전밸브(1번)가 forced/throttle(2번)보다
먼저이므로 이미 보장돼 있었습니다. 의도를 주석으로 명시하고
테스트(16-1)로 고정했습니다 — 장시간 차단된 포지션에 손절이 반복
들어와도 `ALLOW_ESCALATED`로 빠져나갑니다.

### 테스트

`test_partial_fill_lifecycle.py` 140건 → **166건**:
- 13절(7건) 폴링 감시 — SELL 신호 없이도 장시간 차단 감지,
  blocked_since/code 기록, 해소 시 리셋, sync 연결.
- 14절(13건) ERROR 자동 회복 — error_since 기록, 회복 전/후,
  OPEN·FLAT 확정, 매수 차단 해제, acknowledge_error 유지.
- 15절(4건) 강제 매도 실패 escalation.
- 16절(2건) throttle보다 안전밸브 우선, 정상 구간 throttle 동작.

차단 조합 매트릭스는 1P0.5 그대로 유지됩니다 — 모든 조합에서
손절 통과, 5분 후 해제.

**전체 회귀**: 17개 파일 전부 통과, 1개 스킵. `compileall` 정상.

### 배포 시 확인 (우선순위)

1. **`[LIFECYCLE_STUCK]` (CRITICAL)** — 신호와 무관하게 감지되는
   장시간 차단. 뜨면 차단 규칙이 과합니다.
2. **`[FORCED_SELL_FAILED]` (CRITICAL)** — 손절이 브로커에 거부됨.
   시스템이 해결할 수 없으므로 즉시 개입 필요.
3. `[LIFECYCLE_ESCALATED]` — 안전밸브 발동
4. `[LIFECYCLE_ERROR_RECOVERY]` — 자동 회복 빈도
5. `[LIFECYCLE_BLOCK]` 특정 종목 반복 여부
6. `매도가능수량 부족` / `UNEXPECTED_QUANTITY_INCREASE` — 0이어야 정상

---

## 1P0.7: 안전-우선 재설계 — HARD/SOFT 분리, 자동회복 제거 (2026-08-10)

1P0.6에 대한 GPT 코드리뷰 **배포 불가(🔴) 판정** 반영. GPT가 지적한
5개 P0 전부를 재현 후 수정했고, "숫자를 늘리는 방향"이 아니라
**GPT 권장 15단계 중 실행 가능한 항목을 반영해 구조를 단순화**했습니다.
order_id 실연결·브로커 주문조회/취소 연동(GPT 권장 9~11번)은 브로커
API 제약으로 이번 범위 밖입니다 — 이 한계를 명시적으로 남깁니다.

### 근본 문제 진단 (GPT)

> 브로커의 실제 주문 상태를 모르면서 시간과 잔고만으로 주문 종료를
> 추론하고 있기 때문입니다.

1P0.1~1P0.6은 "얼마나 기다리면 풀어줄까"를 계속 정교하게 만들었는데,
그 방향 자체가 틀렸다는 지적입니다. 이번엔 반대로 갑니다 —
**증거 없이는 절대 풀지 않는다.**

### P0-1: BUY_PENDING 중 SELL 미차단 (006360/017900 사고 원인 그대로 재현)

```
BUY174 → broker7 → BUY_PENDING
decide_sell → ALLOW   ← 차단해야 하는데 통과
```
`_evaluate_sell_blocks()`에 `BUY_PENDING` 검사가 아예 없었습니다.
`_evaluate_hard_sell_blocks()`에 추가 — **forced로도 우회 불가**.

### P0-2: `ERROR_AUTO_RECOVERY_SEC=900`은 증거 없는 자동 정상화

```
ERROR(UNEXPECTED_QUANTITY_INCREASE) → 900초 후 → OPEN(qty) 자동 확정
→ BUY guard 해제
```
"15분이 지났다"는 원 주문이 terminal이라는 증거가 아닙니다.
`auto_recover_error()`를 **완전히 제거**했습니다. ERROR는 이제
`acknowledge_error()`로 사람이 실제 계좌를 확인한 뒤에만 해제됩니다.
`ERROR_AUTO_RECOVERY_SEC` 상수도 제거.

### P0-3: `ALLOW_ESCALATED` 자체가 위험 — 5분 경과가 안전을 보장하지 않음

```
SELL353 원 주문 살아 있음 → SELL_PENDING → 5분 경과 → ALLOW_ESCALATED
→ 새 SELL343 → 이중 SELL 가능
```
`SellDecision.ALLOW_ESCALATED`를 **제거**하고
`RECONCILIATION_REQUIRED`로 교체했습니다. HARD block은 5분이 지나도
**여전히 BLOCKED**이며, `RECONCILIATION_REQUIRED`는 매도를 허용하는
게 아니라 CRITICAL 로그로 "사람이 지금 확인해야 한다"를 알릴 뿐입니다.

### P0-4: `forced=True`가 HARD safety까지 우회

```
orphan 상태에서 forced SELL → ALLOW_FORCED
```
GPT 제안대로 차단을 두 종류로 명확히 분리했습니다.

| 분류 | 예 | force 우회 |
|---|---|---|
| **HARD** | BUY_PENDING, SELL_PENDING, orphan, ERROR | **절대 금지** |
| SOFT | retry backoff, MAX_RETRY | 가능 |

`_evaluate_hard_sell_blocks()` / `_evaluate_soft_sell_blocks()`로
분리. `decide_sell()`은 HARD를 먼저 평가하고, HARD가 있으면 forced
여부와 무관하게 `BLOCKED`(5분 경과 시 `RECONCILIATION_REQUIRED`)를
반환합니다 — **손절이어도 여기서 막힙니다(의도된 동작)**. "이미
살아있을 수 있는 주문 위에 새 주문을 더 얹는 것"보다 사람의 확인을
기다리는 편이 안전하다는 GPT의 판단을 그대로 반영했습니다.

### P0-5: orphan 부분잔고 변화/TTL 자동 해제 제거

```
343 → 300 (43주만 추가체결) → orphan clear   ← 300주가 여전히 살아있을 수 있는데 clear됨
ORPHAN_TTL_SEC=600 경과 → 자동 해제           ← TTL도 종료 증거 아님
```
`observe_for_orphan()`을 재설계 — 이 시스템은 항상 전량매도만
하므로(설계 불변), **SELL orphan은 잔고 0 도달**, **BUY orphan은
`expected_final_quantity` 정확 도달**만 자동 해소합니다. 그 외
모든 변화는 진단 로그(`ORPHAN_PARTIAL_OBSERVED`)만 남기고 유지.
`acknowledge_orphan(symbol, note)` 신규 — 사람이 브로커에서 직접
확인한 뒤 근거와 함께 명시적으로 해제(빈 note는 `ValueError`).

### BUY guard 확장 — SELL_PENDING/orphan도 BUY 차단

```
SELL_PENDING → BUY 가능   ← 원 주문 상태를 모르는데 신규 진입 가능했음
SELL orphan  → BUY 가능
```
`would_block_buy_detail()`에 `BLOCK_BUY_WHILE_SELL_PENDING` /
`BLOCK_BUY_ORPHAN_ORDER` 추가. 원 주문 상태를 모르면 해당 종목의
**모든 신규 자동 주문**을 잠급니다(BUY_PENDING/SELL_PENDING/
orphan/ERROR 전부).

### accepted SELL 부작용을 완전 청산 확정 시점으로 지연 (P0)

```python
accepted != full fill
```
`result.accepted=True` 직후 손실 카운트·연속손절·쿨다운·재진입
차단·트레일링 추적·카카오 알림이 **즉시 실행**되고 있었습니다.
047040에서 SELL accepted 후 343주가 남았던 실측 사례가 근거입니다.

`_try_sell_unchecked()`의 accepted 분기를 분리:
- **즉시 유지**: 잔고 캐시 무효화, `_sold_today` 추가(중복 주문
  요청 방지 목적, side effect 아님), `[ORDER]` 로그.
- **완전 청산까지 지연**: `_pending_sell_side_effects[symbol]`에
  `exit_reason`/`avg_buy_price`/`current_price`/`quantity` 저장.
  손실 카운트·재진입 제한·쿨다운·트레일링 추적·카카오 알림 전체를
  `_apply_deferred_sell_side_effects()`로 이동.

`_sync_position_state_machine_shadow`가 `on_sell_result()` 호출 후
lifecycle이 `FLAT`으로 전환된 걸 감지하면(완전 청산 확인)
`_apply_deferred_sell_side_effects()`를 호출합니다.
`resolve_stale_pending()` 타임아웃 경로에서 `broker_quantity<=0`으로
`FLAT`이 확정되는 경우도 동일하게 호출.

### cross-cycle 오염 방지 (재현 확인)

```
이전 거래의 reject_count=5, blocked_since 존재
새 BUY 체결 → OPEN
첫 SELL → 이전 사이클의 blocked_since 때문에 즉시 escalation
```
`_reset_transient_block_state()` 신규 — `sell_reject_*` /
`blocked_since` / `blocked_count` / `last_block_code` /
`last_forced_sell_at` / `error_since` / `orphan_*`를 한 번에 초기화.
**FLAT 확정 시점(사이클 종료)과 새 포지션 OPEN 확정 시점(사이클
시작, `base_quantity_before_order==0`일 때)** 양쪽에서 호출해
어느 경로로 사이클이 바뀌어도 흔적이 남지 않게 했습니다.

### GPT 권장 15단계 중 이번에 반영/미반영

| # | 항목 | 상태 |
|---|---|---|
| 1~3 | BUY_PENDING/SELL_PENDING/ORPHAN·ERROR→BUY = HARD BLOCK | ✅ 반영 |
| 4 | forced가 HARD BLOCK 우회 금지 | ✅ 반영 |
| 5 | 5분 경과도 HARD BLOCK 해제 안 함 | ✅ 반영 |
| 6 | 5분 경과 → RECONCILIATION_REQUIRED + CRITICAL | ✅ 반영 |
| 7 | ERROR 자동 900초 회복 제거 | ✅ 반영 |
| 8 | orphan 부분잔고/TTL 자동해제 제거 | ✅ 반영 |
| 13 | full SELL terminal 후에만 side effect 실행 | ✅ 반영 |
| 14 | 새 cycle 시작/FLAT 확정 시 transient state reset | ✅ 반영 |
| 9~11 | 실제 order_id 연결, 브로커 조회/취소 연동 | ❌ 브로커 API 제약, 범위 밖 |
| 12, 15 | FILLED/CANCELLED 등 terminal 확인, restart reconciliation | ❌ 9~11 선행 필요 |

**9~11번이 없다는 것은 여전히 근본 한계입니다.** `pending_order_id`/
`orphan_order_id`는 지금도 실제 주문 식별자가 아니라 `"pending"`
리터럴입니다. 이번 변경은 "잘못된 자동화를 줄이고 사람이 개입할
지점을 명확히 하는" 방어적 조치이지, 근본 해결(order_id →
status/cancel/terminal reconciliation)을 대체하지 않습니다.

### 테스트 — 성공 조건을 반대로 재작성

GPT 지적대로 "166이라는 숫자가 커져도 안전성 증거가 되지 않는다"에
따라, 기존 테스트가 검증하던 **잘못된 성공 조건 자체를 뒤집었습니다**:

```
이전: BUY_PENDING 중 SELL guard 없음               → PASS 조건
이후: BUY_PENDING 중 SELL은 HARD block             → PASS 조건

이전: orphan 잔고 일부 변화 → 자동 해제             → PASS 조건
이후: 잔고 0(SELL)/목표수량 정확 도달(BUY)만 해제   → PASS 조건

이전: 모든 forced SELL은 통과                       → PASS 조건
이후: HARD block은 forced도 통과 못 함              → PASS 조건

이전: 모든 block은 5분 후 ALLOW_ESCALATED           → PASS 조건
이후: HARD block은 5분 후에도 BLOCKED(RECONCILIATION만 승격) → PASS 조건

이전: ERROR 15분 후 자동 OPEN/FLAT                  → PASS 조건
이후: ERROR는 acknowledge_error()로만 해제           → PASS 조건
```

`test_partial_fill_lifecycle.py` **182건** (섹션 4·7·10·12·14·16
전면 재작성, 17절 신규 7건 — 지연 side-effect 구조 검증).

부수 발견: `test_sold_today_qty_based.py`의 시나리오 6이 SELL 체결
확인 폴링을 생략하고 바로 `_try_buy`를 호출해, 새 SELL_PENDING→BUY
guard에 정상적으로 걸렸습니다. 실제 운영에서는 신호 판단 전에 항상
sync가 먼저 도므로, 테스트에 그 폴링을 시뮬레이션하는 코드를
추가해 실제 흐름과 일치시켰습니다(가드를 약화하지 않음).

**전체 회귀**: 17개 파일 전부 통과(1개 스킵), **GPT가 지적한
"config.settings 의존으로 독립 검증 불가" 문제까지 실제 실행으로
확인**. `compileall` 정상.

### 다음 세션 확인 필요 (배포 전)

1. GPT 재검토 — 이번에도 배포 전 확인 필수
2. `[RECONCILIATION_REQUIRED]` 빈도 — HARD block이 5분을 넘기는
   빈도가 높으면 근본 원인(브로커 응답 지연? 주문 방식?)을 봐야 함
3. order_id 실연결 로드맵 — 다음 단계로 반드시 필요
4. 실계좌 투입 시점 재논의 — 9~11번이 없는 상태이므로 신중 필요

---

## 1P0.7.1: Safety Closure — 남은 P0 두 건 (2026-08-12)

1P0.7에 대한 GPT 재검토("조건부 승인 🟡, 실계좌 배포는 아직 보류")
반영. **범위를 GPT가 지정한 두 P0로 좁혀서** 재현 후 수정했습니다.
threshold나 전략 로직은 변경하지 않았습니다.

### P0-1: BUY zero-fill timeout에서 orphan이 즉시 사라짐 (재현 확인)

`resolve_stale_pending()`의 BUY_PENDING 분기가 0주 체결로
타임아웃되면:
```
BUY_PENDING → orphan 생성(원 주문이 살아있을 수 있음)
→ lifecycle=FLAT(잔고 0)이니까 reset
→ 방금 만든 orphan을 그 reset이 지워버림
```
`_reset_transient_block_state()`가 항상 orphan까지 함께 지웠기
때문입니다. **재현 결과**:
```
BUY 174 accepted → 120초 동안 0주 체결 → 타임아웃
after: FLAT / pending_order_id=None / orphan_order_id=None
BUY guard = None   SELL decide.allowed = True
```
원 BUY 주문이 브로커에 살아있을 수 있는데도 시스템은 "미결 주문
없음"으로 믿는 상태였습니다.

**수정**: `_reset_transient_block_state(state, *, clear_orphan=True)`
에 파라미터 추가. `resolve_stale_pending()`의 BUY_PENDING 분기에서
같은 호출 안에 orphan을 방금 만들었다면 `clear_orphan=False`로
호출해 보존합니다. "FLAT이 됐다"(잔고 0)와 "원 주문이 종료됐다는
증거가 있다"는 다른 이야기라는 GPT의 지적을 그대로 반영했습니다.

**수정 후**:
```
lifecycle: FLAT
orphan_order_id: b1                                    ← 보존됨
BUY guard: BLOCK_BUY_ORPHAN_ORDER(order=b1, age=0s)
SELL decide.allowed: False
```

### P0-2: SELL timeout→orphan→나중 FLAT에서 deferred side-effect 누락 (재현 확인)

8/12 실측(`005935` SELL accepted → 잔고 0 반영까지 약 194초)이
근거입니다. `SELL_PENDING_TIMEOUT_SEC=60`이라 60초 후 `OPEN+orphan`
으로 넘어가는데, 그 뒤 원 주문이 실제로 완전 체결되면:
```
observe_for_orphan() → orphan clear
sync_from_broker()   → OPEN → FLAT   (`else` 분기)
```
경로를 탑니다. `_apply_deferred_sell_side_effects()` 호출이
**`SELL_PENDING` 분기 안에만** 있었기 때문에, `else` 분기로
FLAT이 확정되는 이 경로는 **영원히 호출되지 않았습니다**:
```
after later fill: lifecycle=FLAT, orphan=None
pending_sell_side_effect context = 아직 있음
last_sold_at = None   loss_count = None
```
실제로 완전 청산됐는데도 손실 카운트·연속손절·쿨다운·재진입
차단·트레일링 추적·알림이 전부 적용되지 않는 상태였습니다.

**수정**: 실행 지점을 중앙화했습니다. sync 루프에서 각 종목의
`_lifecycle_before_sync`를 기록해두고, 어떤 경로(SELL_PENDING 즉시
완결 / timeout 후 orphan 해소 / 일반 `sync_from_broker`)로
처리됐든 이 폴링에서 **처음 FLAT이 됐고** 대기 컨텍스트가 있으면
정확히 1회 실행합니다.
```python
if (psm.get(symbol).lifecycle == PositionLifecycle.FLAT
        and _lifecycle_before_sync != PositionLifecycle.FLAT
        and symbol in self._pending_sell_side_effects):
    self._apply_deferred_sell_side_effects(symbol)
```
`_pending_sell_side_effects.pop()`이 이미 1회 소비를 보장하므로
여러 폴링에서 중복 실행되지 않습니다.

### 검증 — 실제 TradingService fixture (GPT 요구 반영)

GPT 지적: "단순 source-string 테스트 말고 실제 서비스 fixture를
넣으십시오. 우리가 1J에서 계속 당했던 '코드와 테스트가 같은 구조적
가정을 공유해서 모두 PASS' 패턴을 여기서 끊어야 합니다."

19절을 `TradingService` + `MockBroker`를 실제로 띄운 통합 테스트로
작성했습니다:
```
초기 100주 OPEN
→ SELL 요청(accepted, 브로커 반영 지연 흉내)
→ 폴링 1회(타임아웃 전) — 컨텍스트 유지 확인
→ 타임아웃 경과 → OPEN+orphan 전환 확인
→ 원 주문이 나중에 실제 완전 체결(잔고 0)
→ FLAT 확정 확인
→ [핵심] pending 컨텍스트 소비, last_sold_at 기록,
  entry_time 제거, 트레일링/손실 로직 반영 확인
→ 같은 FLAT을 여러 번 폴링해도 재실행 없음(C 요구사항)
```
17절의 옛 source-string 검사 2건(`ts_src.count(...)`)은 코드가
중앙화로 옮겨지며 실패 처리됐고, **19절의 실제 실행 검증으로
대체**했습니다.

### P1: deferred side-effect 가격이 실제 체결가가 아님을 명시

```
SELL 요청 시점   10,100원
실제 최종체결    9,900원 (예시, 8/12 194초 지연 사례 근거)
```
근본 해결(실제 fill price 연결)은 아직 없습니다. 컨텍스트 저장
지점과 `_apply_deferred_sell_side_effects()` docstring 양쪽에
`ORDER_PRICE_BASED_ESTIMATE` 주석을 남겨, 이 값이 "실제 체결
손익"이 아니라 주문가 기준 추정치임을 명시했습니다.

### 테스트

`test_partial_fill_lifecycle.py` 182건 → **209건**:
- 18절(8건) BUY zero-fill timeout orphan 보존 — 0주 체결 시 FLAT이면서
  orphan 유지, 잔고>0 부분체결 회귀 없음, `pending_order_id` 애초에
  없는 방어적 케이스.
- 19절(15건) **실제 TradingService 통합 테스트** — P0-2 전체 시나리오.
- 20절(4건) P1 문서화 확인.
- 17절 2건은 중앙화 구조 확인으로 축소, 실질 검증은 19절로 이관.

**전체 회귀**: 17개 파일 전부 통과(1개 스킵). `compileall` 정상.

### 근본 한계 — 이번에도 해결되지 않음 (GPT 명시)

> `pending_order_id`/`orphan_order_id`는 실제 주문번호가 아닙니다.
> `result.order_id`는 trade log/state에는 들어가지만 lifecycle에는
> 연결되지 않습니다. lifecycle과 `_pending_sell_side_effects` 둘 다
> **메모리 전용**입니다 — 프로그램 재시작 시 pending/orphan/deferred
> 컨텍스트가 전부 소실되고, 원 주문은 브로커에서 계속 살아있을 수
> 있습니다.

이번 수정 범위에 포함하지 않았습니다. GPT는 이를 **"무인 실계좌
운영의 배포 blocker"**로 규정했고, 다음 단계로
`order_id → outstanding/order status → cancel → FILLED/CANCELLED/
REJECTED` 연결을 명시적으로 요구했습니다.

---

## 1P0.7.2: BUY side-effect 비대칭 해소 + 문서 정합성 (2026-08-13)

1P0.7.1에 대한 GPT 재검토("두 P0는 승인, 남은 우선순위 9개") 반영.
이번 세션에서는 GPT가 지정한 우선순위 1~3을 처리했습니다.
4~9(order_id 실연결, 브로커 outstanding/status/cancel 인터페이스,
restart reconciliation, 실제 fill price 연결)는 브로커 API 확장이
필요해 이번에도 범위 밖으로 명시적으로 남깁니다.

### 우선순위 1: BUY accepted side-effect를 첫 실체결 확인 시점으로 이연

SELL은 1P0.7에서 이미 "accepted != full fill"로 고쳤는데, **BUY는
비대칭적으로 그대로였습니다.** GPT가 8/12 실측으로 지적:

```
103590
11:47:17  BUY accepted
11:47:27  4주만 체결
11:50:42  83주 전량 확인
```

`entry_time_by_symbol`이 **11:47:17(accepted 시각)**에 즉시 기록돼,
11:52경 "5분 최소수익 미달" 청산이 발동했지만 실제 보유는
**1분 37초**뿐이었습니다. 더 극단적으로는 accepted 후 실제 0주
체결(취소/미체결)이어도 이미 당일 진입횟수·매수 쿨다운·알림이
소모되고 있었습니다.

**수정** — SELL과 대칭 구조로 재구성:
```
BUY accepted        → _pending_buy_side_effects[symbol]에 컨텍스트만 저장
첫 실체결 확인       → _apply_first_fill_buy_side_effects() 실행
  (known_quantity > base_quantity_before_order)
```
`entry_time_by_symbol` / `bought_symbols_today` /
`symbol_entry_count_today` / 매수 알림을 **첫 실체결(first_fill_at)**
시점으로 이연했습니다. `last_order_id_by_symbol`은 즉시 기록(추적
목적이라 지연 불필요), `[ORDER]` 로그도 "접수 완료(미확정)"으로
문구를 바꿔 완료로 오인하지 않게 했습니다.

sync 루프에 감지 로직 추가 — `known_quantity`가
`base_quantity_before_order`(주문 직전 실제 보유수량)보다 늘어난
시점을 첫 체결로 판정합니다. lifecycle 전환(BUY_PENDING→OPEN) 여부와
무관하게(부분체결로 여전히 BUY_PENDING이어도) 동작하며, pop 기반이라
정확히 1회만 실행됩니다.

GPT가 제시한 `first_fill_at`/`full_fill_at` 분리 제안 중,
**`first_fill_at`만 채택**했습니다. "전체 포지션 완전 체결" 기준이
필요한 규칙이 이 코드베이스에 아직 없어 `full_fill_at`을 별도
필드로 두는 건 이번 범위에서 과했다고 판단했습니다 — 필요해지면
`entry_time_by_symbol`과 나란히 추가할 수 있도록 주석에 남겼습니다.

### 우선순위 2: false-pass 수정 + 손실 branch 실행 검증 추가

GPT 지적:
```python
sym19 in svc.state.symbol_trail_loss_at
or svc.state.symbol_loss_count_today.get(sym19, 0) >= 0   # 항상 True
```
`dict.get(key, 0) >= 0`은 키가 없어도 참이라 **사실상 항상
통과하는 false-pass**였습니다. 게다가 19절 fixture(avg=10,000,
sell=10,100)는 수익 거래라 애초에 손실 branch를 지나지 않습니다.

**수정**: 19-12를 "수익 거래이므로 손실 카운터가 증가하지 않는다"는
정확한 검증으로 바꾸고, GPT가 제시한 정확한 fixture(avg=10,000,
sell=9,900 손실, `exit_reason`에 "손절" 포함)로 **21절에 손실 branch
전용 통합 테스트**를 신설했습니다:
```
SELL(손절, 9,900) → timeout → OPEN+orphan → 나중 완전체결(FLAT)
기대: symbol_loss_count_today=1, consecutive_losses=1,
      symbol_stoploss_at 기록, 여러 폴링에도 카운터 중복 증가 없음
```
8건 전부 실제 실행으로 검증합니다.

### 우선순위 3: lifecycle.py 문서가 실제 enforce 상태를 반영하도록 정정

모듈 상단·클래스·메서드 docstring 세 곳이 여전히 "shadow 모드로만
동작"이라고 돼 있었는데, **1P0.2부터 이미 `_try_buy`/`_try_sell`이
실제로 enforce 차단**하고 있어 정반대였습니다. 다음 개발자가
"shadow니까 실제 주문엔 영향 없겠네"라고 오판할 위험이 있었습니다.

정정 내용:
- 현재 enforce 상태임을 명시
- `pending_order_id`/`orphan_order_id`가 아직 `"pending"` 리터럴이지
  실제 주문번호가 아니라는 한계
- lifecycle 상태와 `_pending_sell_side_effects`가 메모리 전용이라
  재시작 시 전부 소실된다는 한계
- 이것이 현재 **무인 실계좌 운영의 배포 blocker**라는 GPT 판정
- 다음 단계(order_id 연결, 브로커 인터페이스 확장, restart
  reconciliation)를 미착수 상태로 명시

### 테스트

`test_partial_fill_lifecycle.py` 210건 → **242건**:
- 21절(11건) 손실 branch 실제 실행 검증(신규).
- 22절(5건) 문서 정합성 확인.
- 23절(16건) BUY 첫 실체결 이연 — 8/12 실측 시나리오(103590) 전 과정
  실제 `TradingService` 통합 테스트, 0주 체결 극단 사례(진입횟수·
  쿨다운·알림 전혀 미소모, pending 컨텍스트가 orphan으로 이관돼
  계속 추적됨).
- 19-12 false-pass 수정.

실제 `TradingService` fixture 작성 중 두 가지를 확인/보정:
- `_sync_position_state_machine_shadow`의 최초 호출은 콜드 초기화
  경로(전체 포지션 일괄 `sync_from_broker`)를 타서 신규 감지 로직을
  건너뜁니다 — 테스트에서 `_try_buy` 전에 초기 동기화를 1회 먼저
  호출해 실제 운영 순서(프로세스 시작 → 초기 동기화 → 신호 평가)와
  맞췄습니다.
- MockBroker는 BUY를 항상 즉시 전량 체결하므로, "결국 취소/미체결로
  끝나는 주문"을 흉내내려면 `_try_buy` 직후 포지션을 강제로 다시
  비워야 했습니다(19절의 SELL 반영 지연 흉내와 동일한 패턴).

**전체 회귀**: 17개 파일 전부 통과(1개 스킵). `compileall` 정상.
threshold나 전략 로직은 변경하지 않았습니다.

### 근본 한계 — 이번에도 해결되지 않음 (GPT 명시, 남은 우선순위 4~9)

```
4. actual order_id lifecycle 연결
5. broker outstanding/status/cancel 인터페이스
6. restart reconciliation / pending context persistence
7. 실제 fill price 연결
8. 그 후 실계좌 shadow/소액 검증
9. 이후 Entry Timing Study 복귀
```

GPT 표현대로, "중복 주문 ↔ 영구 봉쇄" 왕복 문제는 1P0.7부터 상당 부분
정리됐고, 이제부터는 상태머신 규칙을 더 붙이는 단계가 아니라 **실제
브로커 주문 identity와 terminal 상태를 연결하는 단계**로 넘어가는 게
맞다는 판단에 동의합니다. 이 세션은 그 직전 단계(BUY/SELL 대칭성,
false-pass 제거, 문서 정합성)를 마무리하는 데 집중했습니다.

---

## 1P0.7.3: orphan 수동 해제 인터페이스 + 첫 체결 알림 수량 정확성 (2026-08-14)

1P0.7.2 diff에 대한 GPT 재검토 결과, 핵심 수정(BUY side-effect 이연)은
승인됐지만 1P0.8(Broker Order Reconciliation)로 넘어가기 전에 정리할
2가지 지적이 있었습니다. (5분 보유 정책/first_fill_at·full_fill_at
분리는 SELL 로직 변경에 해당해 별도 확인 후 진행 — 이번 단계에는
포함하지 않음)

### 지적 1 (P0): zero-fill orphan의 운영자 해제 경로 부재

`PositionStateMachine.acknowledge_orphan()`은 1P0.7부터 존재했지만,
이 프로세스가 REPL/디버거 연결 없는 백그라운드 asyncio라 실행 중
호출할 인터페이스가 없었습니다. BUY accepted → 실제 0주 체결 →
BUY_PENDING 타임아웃 → orphan이 되면, 프로세스를 재시작하지 않는 한
해당 종목이 영구 HARD block — 그런데 재시작하면 이번엔 outstanding
주문 정보 자체가 소실되는 더 큰 문제(근본 한계 3번)가 생깁니다.

더 중요한 지적: orphan을 해제할 때 `acknowledge_orphan()`은
PositionStateMachine 쪽 상태만 지우고, TradingService가 들고 있는
`_pending_buy_side_effects[symbol]`(SELL orphan이면
`_pending_sell_side_effects[symbol]`)은 그대로 남습니다. 정리하지
않으면 나중에 같은 종목 잔고가 (재진입 등) 다른 이유로 다시 움직였을
때, 오래된 컨텍스트가 그 체결을 "이번 주문의 첫 체결/완전청산"으로
오인할 수 있습니다.

**수정**: `commands/ack_error_{symbol}.json`과 동일한 파일 명령
패턴으로 `commands/ack_orphan_{symbol}.json`을 추가했습니다
(`_process_pending_ack_orphan_commands()`, 매 폴링에서
`_process_pending_ack_error_commands()`와 나란히 호출). 처리 시
`acknowledge_orphan()` 호출 직후 `_pending_buy_side_effects`와
`_pending_sell_side_effects` 양쪽을 모두 정리합니다 — 한 종목이
BUY/SELL pending 컨텍스트를 동시에 갖는 정상 상황은 없으므로, 존재
하지 않는 쪽을 지워도 안전한 no-op입니다. note가 없거나 빈 문자열이면
(`acknowledge_orphan()`의 ValueError) 처리하지 않고 파일만 삭제합니다
(ack_error와 동일한 fail-closed 방식).

사용법 (PowerShell):
```
$body = @{ note = "브로커 콘솔에서 원 주문 취소/미체결 확인" } | ConvertTo-Json
$body | Out-File -Encoding utf8 commands\ack_orphan_475150.json
```

**재검토(같은 날) 추가 P0**: 위 초안을 GPT에게 다시 검토받은 결과,
`acknowledge_orphan()` → `clear_orphan()`이 **orphan이 실제로 없어도
예외 없이 조용히 return**한다는 게 드러났습니다. 즉 정상
BUY_PENDING/SELL_PENDING(orphan 아님)인 종목에 실수로/stale
`ack_orphan_*.json`이 있으면, orphan 해제는 아무 일도 안 하는데
pending 컨텍스트만 삭제돼 "수동 복구 명령이 오히려 정상 주문을
파괴"할 수 있었습니다. `has_orphan_order()`로 현재 orphan이 실제로
있는지 먼저 fail-closed로 확인하도록 추가했습니다 — 없으면 아무
상태도 건드리지 않고 예외만 던져 파일을 삭제합니다.

같은 재검토에서 note 검증도 `str(payload["note"])`이면
`{"note": null}`이 문자열 `"None"`으로 통과하던 것을 확인, 비어있지
않은 문자열만 허용하도록(공백 strip 포함) 강화했습니다.

### 지적 2 (P1): 첫 체결 알림의 수량 표시 오류

`_apply_first_fill_buy_side_effects()`가 첫 부분체결(예: 83주 요청 중
4주) 시점에도 알림/로그에 요청수량(83)을 그대로 써서 "수량 83주"라고
표시했습니다. 실제로는 4주만 보유 중인데 전량 체결로 오인할 수
있었습니다.

**수정**: 폴링 루프에서 실제 체결수량(`known_quantity -
base_quantity_before_order`)을 `filled_quantity` 인자로 넘기도록
시그니처를 변경하고, 로그/알림을 "보유 N주 / 요청 M주"로 구분해
표시합니다. 가격도 SELL 쪽과 동일하게 `ORDER_PRICE_BASED_ESTIMATE`
(주문 시점 가격, 실제 체결가 아님)임을 명시했습니다.

### 테스트

`test_partial_fill_lifecycle.py` 242건 → **278건** (36건 신규):
- 24절(29건): orphan 파일 명령 처리 —
  - 정상 ack로 orphan+pending BUY 컨텍스트 정리(BUY orphan)
  - note 누락/빈 문자열/JSON null/공백뿐인 문자열은 모두 fail-closed로
    거부(`{"note": null}`이 과거 `str(None)="None"`으로 통과하던 버그
    포함), 앞뒤 공백은 strip 후 정상 처리되는지도 회귀 확인
  - SELL orphan에서도 대칭으로 pending SELL 컨텍스트 정리
  - **재검토 P0**: orphan이 실제로 없는 정상 BUY_PENDING/SELL_PENDING
    상태에 stale 명령이 오면 `has_orphan_order()`가 거부하고, lifecycle과
    pending 컨텍스트가 전혀 훼손되지 않는지 BUY/SELL 양쪽 확인
  - 폴링 경로 연결 확인
  - 모두 실제 `TradingService` 통합 fixture로 검증(orphan을 실제로
    만들거나, 일부러 orphan 아닌 정상 PENDING 상태를 만든 뒤 명령을
    실행). `commands/`가 CWD 상대경로라 테스트는 임시 디렉터리로
    chdir했다가 원복합니다.
- 25절(7건): 첫 체결 알림의 수량 표시 — 실제 `_try_buy()`가 계산한
  요청수량을 그대로 읽어와(하드코딩하지 않음) 그보다 작은 부분체결을
  흉내내고, `_notifier.send()`를 가로채 메시지 내용을 직접 검증.

**전체 회귀**: 18개 파일 중 17개 통과, 1개 스킵(`test_legacy_
fixture_structure.py`, 기존과 동일한 사유). `compileall` 정상.

threshold나 전략 로직(RSI/MACD/진입점수 등)은 변경하지 않았습니다.

**known pre-existing failure**: `test_replay_time_axis.py`가 178건 중
10건 실패합니다. 이번 변경분(`trading_service.py`,
`test_partial_fill_lifecycle.py`)과는 무관함을 확인했습니다 — 이
파일은 두 파일 어느 쪽도 import하지 않고, `git stash`로 변경분을
걷어낸 **원본 HEAD `6e45736`**(1P0.7.2, 이번 단계 착수 시점 기준)에서
`python test_replay_time_axis.py` 단독 실행 시 동일하게 178건 중
10건이 실패합니다 — 1P0 계열 변경과 무관한 사전 존재 이슈이며, 이번
단계의 blocker로 취급하지 않았습니다. 원인 조사는 별도 이슈로 남겨둠.
(`websockets` 패키지 미설치로 인한 2개 파일 import 실패는 검증 환경
차이일 뿐이라 별도로 취급 — websockets 설치 후 재실행하면 사라짐.)

### 보류 — SELL 로직 변경이라 확인 필요 → 결론

GPT는 "5분 보유" 규칙(`entry_watch`의 최소수익미달청산)이 여전히
`first_fill_at` 기준이라, 전량체결(`full_fill_at`) 이후로는 여전히
설계 의도보다 훨씬 이른 시점에 발동한다고 지적했습니다.

민우님·GPT 재검토 결론: **`last_filled_at`을 그대로 entry timer로
enforce 전환하지 않습니다.** 이유는 단순히 "보수적으로 미루자"가
아니라, 현재 `last_filled_at`의 의미 자체가 정확하지 않기 때문입니다
— `confirm_buy_from_broker()`는 `broker_quantity ==
expected_final_quantity`일 때만 이 값을 기록하므로, 부분체결→
BUY_PENDING timeout→OPEN+orphan 경로(전량체결이 없는 경로)에서는 이
값 자체가 존재하지 않습니다. 게다가 lifecycle이 아직 메모리 전용이라
재시작하면 이 값도 소실됩니다(근본 한계 2번). 그래서 필요한 개념은
`full_fill_at`이 아니라 **`position_ready_at`**(이 BUY 주문으로 인한
수량 변화가 더 이상 없다고 "확정"된 시점 — 정상 전량체결이면
`full_fill_at`과 같지만, 부분체결 후 잔량이 CANCELLED로 확정되면 그
시점)이며, 이건 실제 주문의 terminal state를 알아야 정확히 잡을 수
있으므로 **1P0.8(actual order_id + get_order_status() +
FILLED/CANCELLED/REJECTED + restart reconciliation)과 자연스럽게
묶입니다.**

1P0.8에서 확정할 예정인 배치:
- `entry_watch`의 급락 손절 / VWAP 이탈(위험관리 성격) → 계속
  `first_fill_at` 기준 유지(1주라도 보유 시작하면 즉시 위험관리 대상)
- `entry_watch`의 5분 최소수익미달청산 → `position_ready_at` 기준으로
  전환(의도한 포지션 구축이 끝난 뒤 5분을 줌)
- 8/12 103590 예시로 검증: accepted 11:47:17 → first_fill 11:47:27
  (위험관리 시작) → full_fill 11:50:42 = position_ready_at(정상
  전량체결이므로) → 최소수익 평가 시작 11:50:42, 최초 평가는
  약 11:55:42. 현재 방식(11:52:27부터 평가)이면 83주 전체 포지션에
  사실상 1분 45초만 준 문제가 해소됨.
- 이 정책 변경 자체(1P0.8에서)도 shadow 관측 없이 곧바로 enforce하지
  않고, 실제 SELL 판단에 반영하기 전 별도 확인을 거칠 예정.

---

## 1P0.8-A.1: 실제 주문번호(order_id)를 PSM에 연결 (2026-08-14)

1P0.7.3 승인 후 GPT가 1P0.8(Broker Order Reconciliation) 착수 전
소스를 직접 검토해 중요한 사실을 확인했습니다: **실제 주문번호를
못 받고 있는 게 아니라, 이미 받고 있는데 연결만 안 돼 있었습니다.**

`KiwoomBroker.place_order()`는 이미 `response.body["ord_no"]`를
`OrderResult.order_id`로 정상 추출해 반환합니다(공식 kt10000/kt10001
매수/매도 API 사용). 문제는 `on_buy_requested()`/`on_sell_requested()`
가 `place_order()` 호출 **전에** PSM에 `"pending"` 리터럴을 먼저
기록하고, 그 뒤로 실제 `result.order_id`가 한 번도 PSM에 다시
연결되지 않았다는 것이었습니다 — 모든 주문이 동일한 `"pending"`
문자열을 공유해 서로 구분이 불가능했고, orphan으로 전이돼도
(`orphan_order_id = pending_order_id`) 마찬가지였습니다.

**수정**: `PositionStateMachine.confirm_pending_order_id(symbol,
order_id)`를 새로 추가하고, `place_order()` accepted 직후(BUY/SELL
공통) 호출해 `"pending"` placeholder를 실제 `ord_no`로 교체합니다.

- BUY: `on_buy_result(symbol, result.accepted)` 직후 accepted면 호출.
- SELL: `on_sell_result(accepted=True, ...)`는 다음 폴링까지 지연
  호출되므로(체결 여부는 브로커 잔고 재조회로만 확인), place_order()
  accepted 분기의 `else:`(기존 `_forced_sell_failures` 정리 위치)에서
  별도로 호출.
- accepted인데 `order_id`가 비어있는 이상 응답은 추측하지 않고
  `"UNKNOWN_ORDER_ID"` sentinel + `last_error =
  "ACCEPTED_WITHOUT_ORDER_ID"`로 명시적으로 남깁니다. lifecycle을
  ERROR로 전이시키지는 않습니다 — 그러면 이 종목의 강제 손절 SELL까지
  HARD block되어 order_id 하나 놓친 것보다 더 위험한 상황이 됩니다.
- 이미 다른 사유(거부 등)로 `pending_order_id`가 `None`이면 no-op
  (레이스 방지).

`lifecycle.py` 모듈 docstring의 "근본 한계" 목록도 정정: order_id
**연결**은 됐지만, 이 번호로 실제 조회/취소하는 Broker 인터페이스
(`get_open_orders()`/`get_order_status()`/`cancel_order()`)는 아직
없다는 것을 명확히 구분해서 남겼습니다 — 식별자 연결과 실제
reconciliation은 별개입니다.

**범위 밖으로 명시적으로 남긴 것**: 이 번호로 무언가를 조회/판단하는
로직은 이번 단계에 전혀 없습니다. `get_open_orders()`/
`get_order_status()`(ka10075/ka10076 기반)와 `cancel_order()`
(kt10003)는 GPT 제안대로 다음 하위 단계(1P0.8-B 이후)에서, 실제 키움
모의계좌 응답을 먼저 캡처·검증한 뒤 진행합니다 — 필드 의미를
추측하지 않습니다.

### 테스트

`test_partial_fill_lifecycle.py` 278건 → **294건** (16건 신규, 26절):
- PSM 단위: accepted 후 실제 order_id로 교체, 거부로 이미 None이면
  no-op, order_id 빈 문자열이면 sentinel + last_error 기록하되
  lifecycle은 ERROR로 전이하지 않음(BUY_PENDING 유지 확인)
- 실제 `TradingService`(MockBroker) 통합: BUY/SELL 양쪽 모두 accepted
  직후 pending_order_id가 더 이상 `"pending"`이 아니라 실제 MockBroker
  주문번호(`MOCK-######`) 형식임을 확인
- zero-fill BUY timeout → orphan 시나리오에서 orphan_order_id도 실제
  주문번호를 물려받는지 확인
- 22절 기존 체크(22-3)를 새 문서 상태에 맞게 갱신(예전엔 "여전히
  pending 리터럴"을 확인했는데 이제는 사실이 아니므로) + 22-3b 신설
  (조회/취소 인터페이스는 여전히 없다는 한계는 그대로 확인)

**전체 회귀**: 18개 파일 중 17개 통과, 1개 스킵(기존과 동일).
`test_replay_time_axis.py`는 여전히 same known pre-existing failure
(위 1P0.7.3 절 참고, 이번 변경과 무관 재확인). `compileall` 정상.

threshold나 전략 로직(RSI/MACD/진입점수 등)은 변경하지 않았습니다 —
이 단계는 순수하게 이미 존재하던 데이터(order_id)를 올바른 곳에
연결하는 배관(plumbing) 작업입니다.

### 다음 (1P0.8-B 이후)

```
- Broker 추상 인터페이스에 get_open_orders()/get_order_status()/
  cancel_order() 계약 추가, KiwoomBroker + MockBroker 양쪽 구현
- 내부 표준 BrokerOrder/OrderStatus 모델(PENDING/PARTIALLY_FILLED/
  FILLED/CANCELLED/REJECTED/UNKNOWN) — 키움 원시 응답과 분리
- 실제 키움 모의계좌에서 BUY 발행 → ka10075(미체결) → 체결 후
  ka10075/ka10076(체결) 응답을 먼저 캡처·검증 (민우님이 실측 제공
  가능하다고 하셨음)
- get_order_status()는 "미체결 목록에 없다"만으로 FILLED 단정하지
  않음 — 체결 조회와 함께 판단, 모호하면 UNKNOWN으로 fail-close
- cancel_order()(kt10003)는 조회 API 충분히 검증한 뒤 가장 마지막에
- 이후 restart reconciliation(잔고+outstanding orders+저장된 pending
  context 대조), 실제 fill price 연결, position_ready_at 도입
```

### 근본 한계 — 이번에도 해결되지 않음 (GPT 명시, 남은 우선순위 4~9)

```
4. actual order_id lifecycle 연결
5. broker outstanding/status/cancel 인터페이스
6. restart reconciliation / pending context persistence
7. 실제 fill price 연결
8. 그 후 실계좌 shadow/소액 검증
9. 이후 Entry Timing Study 복귀
```

이번 단계는 1P0.8 착수 전 마지막 정리였습니다. GPT 최종 판정대로,
다음 단계는 실제 주문 identity/status/cancel 연결(1P0.8)이며,
`first_fill_at`/`full_fill_at` 명시적 분리도 그 작업에 함께 넣는 것이
좋다는 제안을 받았습니다(정책 확정 후).

### 1P0.8-A.1 재검토 — 커밋 전 최종 보강 2건 (2026-08-14)

위 구현을 커밋하기 직전 GPT가 소스를 한 번 더 검토해 작은 구멍 2개를
지적했습니다. 둘 다 순수 plumbing/관측성 보강이며 BUY/SELL/HOLD
판단 로직은 전혀 건드리지 않았습니다.

1. **`order_id` 공백 정규화 누락**: `confirm_pending_order_id()`가
   전달받은 `order_id`를 `strip()`하지 않아, 공백만 있는 문자열
   (`"   "` 등)이 "값이 있는" 정상 order_id로 오인되어 저장될 수
   있었습니다. `state.pending_order_id = "UNKNOWN_ORDER_ID"` sentinel
   경로가 트리거되지 않고 조회 불가능한 문자열이 그대로 남는 문제.
   → 메서드 진입 직후 `order_id = str(order_id or "").strip()`을
   추가해, 앞뒤 공백은 제거하고 공백만 있던 값은 빈 문자열로 정규화한
   뒤 기존 sentinel 로직을 그대로 타도록 수정.

2. **`order_id` 누락 시 실제 CRITICAL 로그 부재**: 메서드 docstring은
   "CRITICAL 로그로 노출"한다고 적혀 있었지만, 실제 구현은
   `_log_event()`(CSV 이벤트 기록)만 호출하고
   `self.app_logger.critical()`은 한 번도 호출하지 않았습니다 —
   운영자가 즉시 인지할 CRITICAL 알림이 실제로는 없었던 것입니다.
   → PSM(`confirm_pending_order_id`)은 상태 관리만 계속 책임지고,
   CRITICAL 알림은 호출부인 `TradingService`가 `result.order_id`를
   직접 확인해 남기는 구조로 분리했습니다. `_try_buy()`(BUY,
   `confirm_pending_order_id()` 호출 직후)와
   `_try_sell_unchecked()`(SELL, accepted 분기의
   `confirm_pending_order_id()` 호출 직후) 양쪽에 각각
   `if not str(result.order_id or "").strip():
   self.app_logger.critical(f"[ORDER_ID_MISSING] {symbol} | 주문
   accepted=True지만 order_id가 비어 있음 | side=BUY|SELL")`를
   추가.

추가로, `lifecycle.py` 모듈 docstring의 하위 "다음 단계" 문단이
여전히 order_id 연결을 "미착수" 상태로 서술하고 있던 stale 문구도
발견돼 함께 정정했습니다 — 위쪽 "근본 한계" 목록은 이미
1P0.8-A.1 완료를 반영하고 있었지만, 아래쪽 문단이 갱신되지 않아
같은 문서 안에서 서로 모순된 상태였습니다.

**테스트**: `test_partial_fill_lifecycle.py` 294건 → **306건**
(12건 신규, 27절):
- PSM 단위: 공백만 있는 order_id → sentinel 처리(27-1, 27-2), 앞뒤
  공백이 있는 정상 order_id는 strip되어 저장(27-3)
- 실제 `TradingService`(MockBroker, `place_order()`가 빈 order_id를
  반환하도록 몽키패치) 통합: BUY accepted+order_id 빈 값 →
  `app_logger.critical()`이 정확히 1회 호출되고 메시지에
  `ORDER_ID_MISSING`/종목코드/`side=BUY` 포함 확인(27-4, 27-5),
  SELL도 동일하게 `side=SELL`로 확인(27-6, 27-7)
- 회귀 방지: 정상 order_id가 있는 정상 BUY 흐름에서는
  `app_logger.critical()`이 전혀 호출되지 않음(27-8)
- 소스 검증: strip() 정규화 로직 존재(27-9), BUY/SELL 양쪽 모두
  `ORDER_ID_MISSING` CRITICAL 로그 호출 존재(27-10, 27-11), stale
  docstring 문구가 정정됨(27-12)

**전체 회귀**: 18개 파일 중 17개 통과, `test_replay_time_axis.py`만
동일한 기존 pre-existing failure(178건 중 10건, 이번 변경과 무관,
위 1P0.7.3 절 참고). `compileall` 정상.

이 보강으로 GPT가 커밋 전 요구한 4개 체크리스트(① order_id 공백
normalize, ② order_id 누락 시 실제 CRITICAL 로그, ③ lifecycle.py
stale 문구 수정, ④ 관련 테스트 추가)가 모두 완료되어 **1P0.8-A.1을
최종 종료**합니다. 다음 단계는 GPT 제안대로 **1P0.8-B.1**: 실제
키움 모의계좌에서 ka10075(미체결)/ka10076(체결) 원시 JSON 응답을
읽기 전용으로 캡처하는 진단 스크립트입니다 — `get_open_orders()`/
`get_order_status()`/`cancel_order()` 실제 구현이나 SELL/BUY 판단
변경은 이번에도 범위 밖입니다.

---

## 1P0.8-B.1: ka10075/ka10076 read-only 진단 프로브 (2026-08-14)

1P0.8-A.1 종료 직후, 민우님이 실제 키움 공식 REST 가이드(모의투자
도메인 `mockapi.kiwoom.com`, ka10075/ka10076 공식 코드 샘플과 예시
JSON 포함)를 근거로 1P0.8-B.1의 확정 설계를 직접 제시했습니다. 이번
단계는 그 사양을 그대로 구현한 것이며, 별도 검토 라운드 없이 바로
반영했습니다.

**목적**: `get_open_orders()`/`get_order_status()` 같은 도메인
인터페이스를 설계하기 전에, 실제 키움 모의계좌가 미체결(`ka10075`)/
체결(`ka10076`) 조회에 대해 어떤 원시 응답을 주는지부터 확보합니다.
raw 응답을 보기 전에 우리 쪽 상태 모델(PENDING/FILLED/CANCELLED 등)
을 먼저 설계하면, 키움 응답을 우리 예상에 끼워 맞추는 위험이 있기
때문입니다(민우님 설계 사유).

### 새 파일

- **`tools/order_reconciliation_probe.py`** — read-only CLI 진단
  스크립트. `--symbol`/`--order-id`로 지정한 종목·주문에 대해
  T+0/1/3/10초(기본, `--intervals`로 조정 가능) 시점마다 ka10075,
  ka10076을 각각 호출해 JSONL 한 줄에 한 API 호출씩 기록합니다.
- **`test_order_reconciliation_probe.py`** — 50건, 아래 12개 안전
  요건을 전부 커버.

### 구조적 안전장치 (민우님 사양 그대로)

1. **주문 기능 없음**: `place_order()`/`cancel_order()`를 이 스크립트
   가 직접 호출하지 않습니다. 주문은 항상 기존 프로그램이나 HTS/
   영웅문에서 사람이 먼저 넣고, 그 `ord_no`를 `--order-id`로 전달하는
   구조라 이 진단기가 매매 사고를 일으킬 가능성이 구조적으로 0에
   가깝습니다.
2. **mock 도메인만 허용**: `settings.broker.base_url`의 host가 정확히
   `mockapi.kiwoom.com`이 아니면 즉시 거부(`assert_mock_domain`).
   부분 문자열 포함이 아니라 정확히 일치해야 통과 — `mockapi.kiwoom.
   com.evil.example` 같은 우회도 차단합니다. 우회 옵션(플래그로 검사
   끄기)은 의도적으로 만들지 않았습니다.
3. **api-id 허용 목록**: `ALLOWED_API_IDS = {"ka10075", "ka10076"}`.
   다른 값이 들어오면(코드 수정 실수 포함) 네트워크 호출 자체를 하지
   않고 즉시 `DisallowedApiId` 예외.
4. **`order_id` fail-closed 검증**: `None`/빈 문자열/공백뿐/
   `"pending"`/`"UNKNOWN_ORDER_ID"`는 모두 거부(`validate_order_id`).
   뒤의 두 값은 PositionStateMachine이 남기는 placeholder/sentinel
   리터럴(1P0.8-A.1)이며 실제 주문번호가 아닙니다. 참고로 ka10075/
   ka10076 요청 바디 자체가 종목코드 기준 조회라 `order_id`를 키움에
   보내는 파라미터로 쓰지도 않습니다 — `--order-id`는 JSONL 레코드에
   붙는 라벨일 뿐입니다.
5. **민감정보 미기록**: 요청 헤더 전체를 절대 덤프하지 않고,
   응답 헤더는 `api-id`/`cont-yn`/`next-key`만 allow-list로 뽑습니다.
   응답 바디는 원본을 그대로 저장하되(가공 없음 원칙), `acnt_no`
   등 계좌번호로 보이는 키·`token`/`appkey`/`secretkey` 계열 키는
   값만 `"[REDACTED]"`로 치환합니다(`redact_sensitive`) — 실측 ka10075
   응답 예시에 `acnt_no`가 실제로 포함돼 있음을 확인했고, 계좌정보
   비저장은 raw-dump 원칙보다 우선하는 절대 원칙이기 때문입니다.
6. **실패를 위장하지 않음**: 네트워크 예외(타임아웃/연결 실패 등)는
   삼키지 않고 `http_status=None` + `{"probe_transport_error":
   "<예외 타입명>"}`으로 명시적으로 남깁니다. 예외 원문 문자열은
   기록하지 않습니다(일부 requests 예외는 문자열에 요청 정보가 섞여
   나올 수 있어 타입명만 사용). 업무 오류(`return_code != 0`)나
   HTTP 오류(4xx/5xx)도 예외를 던지지 않고 그대로 반환·기록합니다 —
   "조회 실패"라는 신호 자체가 중요한 데이터이기 때문입니다.
7. **`Broker` 추상 인터페이스는 건드리지 않음**: `get_open_orders()`
   같은 정식 계약을 아직 추가하지 않았습니다. `KiwoomBroker`의 기존
   `authenticate()`/`session`/`access_token`만 재사용해 원시 HTTP
   호출을 별도로 수행합니다.

### JSONL 스키마 (민우님 확정 사양)

한 줄 = API 호출 하나. `schema_version`/`run_id`/`scenario`/
`captured_at_kst`/`elapsed_ms`/`environment`/`symbol`/`side`/
`order_id`/`requested_quantity`/`api_id`/`request_body`/`http_status`/
`response_headers`/`response_body`. 상태값(`status`/`filled_qty`/
`remaining_qty` 등) 같은 정규화 필드는 의도적으로 아직 만들지
않았습니다 — 실측 후 1P0.8-B.2에서 설계합니다.

### 테스트: `test_order_reconciliation_probe.py` 50건 (신규 파일)

민우님이 제시한 12개 체크리스트를 절 번호로 그대로 매핑:
mock 도메인 정확 일치·우회 거부(1), 허용 안 된 api-id 즉시 거부(2),
authorization/토큰이 결과 어디에도 없음(3), 리스트에 중첩된 `acnt_no`
까지 재귀 redaction(4), `UNKNOWN_ORDER_ID`/`pending`/`None` 거부(5),
공백뿐인 order_id 거부(6), 비민감 필드·리스트 순서/길이 보존(7),
`cont-yn`/`next-key` 값 보존(8), 업무 오류·HTTP 500 응답도 예외 없이
반환(9), `main()` 통합 테스트로 호출 1회=JSONL 1줄 보장(10), run_id/
elapsed_ms/schema_version 필드 검증(11), 네트워크 예외 시 조용히
정상값으로 위장하지 않음(12). 추가로 소스 레벨 구조 검증(13):
`.place_order(`/`.cancel_order(` 실호출 없음, `PositionStateMachine`/
`TradingService` import 없음, 요청 바디에 `order_id` 파라미터 없음.

전체 회귀: `run_regression_tests.py` 19개 파일 중 18개 통과(신규
테스트 파일 포함, 자동 발견됨). `test_replay_time_axis.py`만 기존
pre-existing failure(이번 변경과 무관, 178건 중 10건, 위 절 참고).
`compileall` 정상.

### `.gitignore`

`diagnostics/`를 추가했습니다 — 이 프로브가 만드는 JSONL은
민감정보가 redact되긴 하지만 실제 종목/주문번호/시각 등 실거래
활동 패턴을 담고 있어 커밋 대상이 아닙니다.

### 범위 밖으로 명시적으로 남긴 것

- `BrokerOrder`/`OrderStatus` 내부 표준 모델, `PENDING`/
  `PARTIALLY_FILLED`/`FILLED`/`CANCELLED`/`REJECTED`/`UNKNOWN` 매핑
  — 실측 JSONL을 먼저 확보한 뒤 1P0.8-B.2에서.
- `Broker.get_open_orders()`/`get_order_status()`/`cancel_order()`
  정식 계약 — 1P0.8-C 이후.
- "ka10075에서 사라짐 = FILLED" 같은 해석/추론 — 이 프로브는 절대
  하지 않습니다. `ka10076`(체결이력)과 교차 확인해야 판단 가능하다는
  것은 민우님이 명시적으로 강조했고, 그 판단 로직 자체가 아직
  존재하지 않습니다(원시 데이터만 모으는 단계).

### 다음 (1P0.8-B.2 이후, 민우님 확정 순서)

```
1P0.8-B.1 (완료): raw diagnostic → 실제 모의계좌 JSONL 획득 (민우님이 로컬에서 실행)
1P0.8-B.2: 획득한 JSONL을 fixture로 고정 → BrokerOrder 모델 설계 →
           PENDING/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/UNKNOWN 매핑
1P0.8-C: Broker read interface (get_open_orders/get_order_status)
1P0.8-D: PSM reconciliation
1P0.8-E: restart reconciliation
그 후: cancel_order
```

이 프로브 자체는 실행해도 아무 것도 안전하게 바뀌지 않습니다(read-
only, TradingService/PSM 미접촉). **다음 세션에서 필요한 것은 민우님이
실제 모의계좌에서 이 스크립트를 실행해 JSONL을 확보하는 것**입니다 —
전량체결/미체결(지정가, 취소는 HTS에서 사람이 수행)/가능하면
부분체결까지 세 가지 사례를 모으는 것을 권장합니다(민우님 설계 참고).

---

## 1P0.8-B.1 재검토 — 실측 캡처 전 보강 2건 (2026-08-14)

민우님이 실제 모의계좌 캡처를 시작하기 전, B.2의 근거 데이터 신뢰성과
직결되는 구멍 2개를 GPT가 지적했습니다. 둘 다 read-only 원칙과
Broker 인터페이스 미변경 원칙은 그대로 유지한 채 보강했습니다.

1. **mock-domain fail-closed 강화 + defense-in-depth**: 기존
   `assert_mock_domain()`은 host만 확인해서 `http://mockapi.kiwoom.
   com`(평문 전송, 토큰이 암호화 없이 나갈 위험)도 통과했고, `main()`
   에서만 호출돼서 누군가 `call_kiwoom_api()`를 직접 다른 base_url로
   호출하면(예: 실전 URL 하드코딩 실수) 우회가 가능했습니다.
   → `assert_mock_domain()`이 이제 scheme(`https` 고정)·host(정확히
   `mockapi.kiwoom.com`)·port(명시 안 됨 또는 443만 허용) 세 가지를
   모두 검증합니다. 그리고 `call_kiwoom_api()` 맨 앞에서도 다시
   호출해, 호출부가 이 검증을 우회해도 실제 네트워크 계층에서 한 번
   더 막히도록 했습니다.

2. **연속조회(pagination) 처리 누락**: ka10075/ka10076은 `--order-id`
   로 특정 주문 하나만 필터링하는 게 아니라 종목 기준 목록을
   반환하는데, 기존 코드는 매번 `cont-yn=N`/`next-key=""`로만 요청하고
   응답의 `cont-yn=Y`(다음 페이지 존재)를 그냥 JSONL에 기록만 하고
   끝냈습니다. 목표 주문이 두 번째 페이지 이후에 있으면 "이 주문이
   미체결 목록에 없었다"가 아니라 "첫 페이지만 봤다"를 "없었다"로
   오인하는 위험한 데이터가 만들어질 수 있었습니다.
   → `call_kiwoom_api()`가 `cont_yn`/`next_key` 파라미터를 받도록
   확장하고, 새 함수 `fetch_paginated()`가 응답의 `cont-yn=Y`를
   따라가며 `next-key`를 다음 요청에 그대로 실어 모든 페이지를
   순서대로 조회합니다. 상태 해석은 여전히 하지 않고, 각 페이지를
   그대로 JSONL 한 줄씩으로 남기되 `page_index`/`request_cont_yn`/
   `request_next_key` protocol metadata를 추가해 어떤 요청의 응답인지
   JSONL만 보고 알 수 있게 했습니다. 무한 루프 방지용
   `MAX_CONTINUATION_PAGES`(기본 20, `--max-pages`로 조정 가능) hard
   cap을 두고, cap에 도달했는데도 더 볼 데이터가 있다면(`cont-yn=Y`)
   추가 네트워크 호출 없이 `probe_page_cap_exceeded=True`를 명시하는
   synthetic 레코드를 하나 더 남기고 멈춥니다 — 침묵 없이 중단.

### 테스트: `test_order_reconciliation_probe.py` 50건 → **78건** (28건 신규)

- 14절(6건): scheme=http, 비표준 포트(8443/80), 잘못된 scheme(ftp)
  거부, 정상 URL(포트 없음/443)은 통과.
- 15절(2건): `call_kiwoom_api()`에 직접 실전 URL/http URL을 넘겨도
  거부되고 네트워크 호출이 0회임을 확인(main()의 검증 우회 시나리오).
- 16절(8건): `fetch_paginated()`가 cont-yn=Y,Y,N 3페이지를 순서대로
  조회, 각 요청이 직전 응답의 next-key를 정확히 실어 보내는지, 실제
  HTTP 요청 헤더까지 확인.
- 17절(6건): 항상 cont-yn=Y를 반환하는 fake session으로 무한
  continuation을 흉내내 `max_pages=3`에서 실제 호출이 정확히 3회로
  제한되고, synthetic cap 레코드(http_status=None,
  `probe_page_cap_exceeded=True`, page_index=4)가 남는지 확인.
- 18절(6건): `main()` 통합 테스트로 ka10075가 2페이지(Y→N),
  ka10076이 1페이지인 시나리오에서 JSONL에 정확히 3줄이 만들어지고,
  각 페이지의 request_cont_yn/request_next_key/response_body가
  올바르게 분리·보존되는지 확인.

기존 1~13절(50건)은 시그니처 변경(`call_kiwoom_api`에 `cont_yn`/
`next_key` 키워드 인자 추가, `build_record`에 `page_index`/
`request_cont_yn`/`request_next_key` 필드 추가)에도 전부 하위 호환
방식으로 유지되어 수정 없이 그대로 통과합니다.

**전체 회귀**: 19개 파일 중 18개 통과. `test_replay_time_axis.py`만
기존 pre-existing failure(이번 변경과 무관). `compileall` 정상.

### 범위는 그대로 지켰습니다

`BrokerOrder`/`OrderStatus`/`get_order_status()` 등은 여전히 만들지
않았습니다 — 이번 보강은 read-only 프로브 자체의 안전성과 데이터
완전성(pagination 누락 방지)에 대한 것이지, 도메인 모델 설계를
앞당긴 것이 아닙니다. GPT도 이 두 건 외에는 "현재 구조를 유지"라고
확인했습니다.

이걸로 1P0.8-B.1은 실측 캡처를 시작해도 되는 상태입니다. 다음은
민우님이 실제 모의계좌로 프로브를 실행해 JSONL을 확보하는 것입니다.

---

## 1P0.8-B.1 실측 중 발견 — .env 미로딩 버그 수정 (2026-08-14)

민우님이 실제 모의계좌(삼성전자 157897, 매수 5주 체결)로 프로브를
처음 실행했을 때 인증 단계에서 다음 오류로 실패했습니다:

```
RuntimeError: token request failed: {'return_msg': '인증에 실패했습니다
[8001:App Key와 Secret Key 검증에 실패했습니다]', 'return_code': 3}
```

**원인**: 계정/키 등록 문제가 아니라 이 프로브 자체의 버그였습니다.
`config/settings.yaml`은 `app_key: ${KIWOOM_APP_KEY}` 식으로 실제
값을 환경변수 치환에 의존하는데(`config/settings.py`의
`_substitute_env`), 이 치환은 환경변수가 없으면 **예외를 던지지
않고 조용히 빈 문자열로 대체**합니다. `app/main.py`는 실행 전에
자체 `load_dotenv()`를 호출해 `.env`를 `os.environ`에 채운 뒤
`load_settings()`를 호출하는데, `tools/order_reconciliation_probe.
py`에는 이 호출이 아예 빠져 있었습니다 — 그래서 셸에
`KIWOOM_APP_KEY`/`KIWOOM_SECRET_KEY`가 전역으로 설정돼 있지 않으면
빈 App Key/Secret Key로 토큰을 요청했고, 키움은 정확히 이 오류로
거부했습니다.

**수정**: `app/main.py`의 `load_dotenv()`와 동일한 구현을
`order_reconciliation_probe.py`에 추가하고, `main()`에서
`load_settings()` 호출 **전에** 반드시 실행하도록 했습니다. `.env`
경로는 `--env-file`로 조정 가능(기본 `.env`, 프로젝트 루트 기준).
`main()`은 이제 이 순서를 지킵니다: CLI 파싱 → order_id/intervals
검증 → **`load_dotenv()`** → `load_settings()` → mock-domain 검증 →
브로커 인증.

### 부수 발견 — `test_partial_fill_lifecycle.py`의 실행 시각 의존 flaky 4건

위 수정을 검증하러 전체 회귀를 다시 돌리는 과정에서, `test_partial_
fill_lifecycle.py`(1P0.8-A.1에서 작성)의 26/27절 중 BUY 관련 통합
테스트 4곳이 **KST 14:50 이후에 실행하면 실패**하는 걸 발견했습니다.
`_try_buy()`에는 "14:50 이후 신규매수 차단" 게이트가 있는데(6차
GPT 코드리뷰에서 이미 한 번 문제 됐던 지점 — 주석에도 "1B.6절에서
재현 확인"이라고 적혀 있음), 23절/24절/26절 앞부분의 통합 테스트는
`domain.service.trading_service.now_kst`를 고정 시각으로 monkeypatch
해서 이 문제를 피했는데, 26절 후반부(BUY/orphan 통합, 26-8~26-9)와
27절의 BUY 관련 3곳(27-4/27-5, 27-8)은 이 patch 없이 실제
`_try_buy()`를 호출하고 있었습니다 — 작성 당시(오전 시간대)엔
우연히 통과했을 뿐, 원래도 flaky였습니다. 5곳 전부 23절과 동일한
패턴(`with patch("domain.service.trading_service.now_kst",
return_value=datetime(2026, 8, 12, 11, 47, 17)):`)으로 시각을
고정해 수정했습니다. SELL 경로(`_try_sell`)는 이 게이트가 없어 원래
영향받지 않았습니다.

이건 이번 세션의 코드 변경(1P0.8-B.1)과는 무관하고, 이전에 작성된
테스트의 잠재적 결함이 시간이 지나 실제로 발현된 것입니다 — 회귀
검증을 실행 시각과 무관하게 항상 통과하도록 고쳤습니다.

### 테스트

- `test_partial_fill_lifecycle.py`: 306건 그대로(신규 추가 없음,
  기존 5개 호출부에 시각 고정만 추가) — 전부 통과, 실행 시각과 무관.
- `tools/order_reconciliation_probe.py`에 대한 신규 테스트 6건 추가
  (`test_order_reconciliation_probe.py` 78건 → **84건**): `.env`
  파싱(주석/빈 줄 skip, setdefault로 기존 값 보존, 파일 없어도 예외
  없음), 소스 레벨로 `load_dotenv()`가 `load_settings()`보다 먼저
  호출되는지 확인, `main()` 통합 테스트로 실제 실행 순서(.env 로드 →
  settings 구성)를 실증.

**전체 회귀**: 19개 파일 중 18개 통과. `test_replay_time_axis.py`만
기존 pre-existing failure(무관). `compileall` 정상.

민우님은 이제 다시 실행하시면 됩니다. `.env`가 프로젝트 루트에
있는지만 확인해주세요(같은 디렉터리에서 `python -m tools.
order_reconciliation_probe ...`로 실행).

---

