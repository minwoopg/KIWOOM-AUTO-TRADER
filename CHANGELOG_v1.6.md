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
