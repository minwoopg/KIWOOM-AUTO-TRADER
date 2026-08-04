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

