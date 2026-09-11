from __future__ import annotations

"""2026-09-11 (B01, 개선 체크리스트 0단계): 실행 기준선(run baseline) 기록.

배경 — 왜 필요한가:
지금까지 하루 성과를 분석할 때 "이 거래가 그 시점의 어떤 코드/설정으로
나온 것인가"를 확실히 답할 방법이 없었습니다. 여러 라운드의 diff를
전달하고 민우님이 순서대로 로컬에 적용해온 이 프로젝트 특성상, 실제
운영 시점에 어떤 커밋이 반영돼 있었는지·설정이 리뷰 시점과 같았는지가
번들 분석마다 암묵적 가정으로 남아 있었습니다(예: 2026-08-21 사실관계
정정 — E.1-A가 실제로 언제부터 적용됐는지 문서 서술과 실제가 어긋난
적이 있었음).

이 모듈은 프로세스 시작마다 다음을 기록합니다: 실행을 구분하는
run_id, 코드 커밋 SHA와 미커밋 변경 여부, 실효 설정의 식별 해시,
모의/실전 구분, 시작 시각(KST). 트레이딩 로직에는 전혀 관여하지
않는 순수 관측 계층입니다.

기존 CSV(trades.csv/signal_log.csv)와의 연결 방법(중요 설계 결정):
이 모듈은 기존 TRADE_FIELDS/SIGNAL_FIELDS 스키마에 새 컬럼을 추가하지
않습니다. 두 파일은 프로세스 재시작과 무관하게 계속 이어지는 단일
누적 파일이라(logs/trades.csv, logs/signal_log.csv — 날짜별로 파일이
새로 생기지 않음), 이미 오래전에 고정된 헤더 행 위에 새 컬럼을 가진
행을 이어붙이면 헤더보다 컬럼 수가 많은 비정형 CSV가 되어
`export_daily_bundle.py`나 분석 도구의 파서가 깨질 위험이 있습니다
(실제로 이번 확인 요청에서 지적된 지점). 대신 이 모듈은 완전히 새
파일(run_baseline.csv, 이번에 새로 생기므로 레거시 헤더 문제가
없음)에 실행마다 한 행씩 남기고, 거래/신호 로그는 각자의 `timestamp`
컬럼과 이 파일의 `started_at`을 시간 범위로 대조해서 연결합니다 —
`resolve_run_id_for_timestamp()`가 이 조인을 구현합니다.
"""

import hashlib
import json
import subprocess
import sys
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.time_utils import KST_TZ, now_kst

# ── 민감정보 redaction ──────────────────────────────────────────
# 명시적 denylist(현재 Settings에 실제로 존재하는 필드) +
# 이름 패턴 휴리스틱(향후 추가되는 필드에 대한 방어) 이중 적용.
# "API 키·토큰·계좌번호 등 민감정보는 기록하지 마세요" 원칙 준수 —
# 해시에 넣는 값에도, 사람이 읽는 덤프에도 원문이 들어가지 않습니다.
_SENSITIVE_DOTTED_PATHS = {
    "broker.app_key",
    "broker.secret_key",
    "broker.account_number",
    "websocket.app_key",
    "websocket.secret_key",
    "kakao.access_token",
    "kakao.refresh_token",
    "kakao.rest_api_key",
    "kakao.client_secret",
}
_SENSITIVE_NAME_PATTERNS = ("key", "secret", "token", "account_number", "password")
_REDACTED_PLACEHOLDER = "***REDACTED***"


def _is_sensitive_field_name(name: str) -> bool:
    lname = name.lower()
    return any(pat in lname for pat in _SENSITIVE_NAME_PATTERNS)


def _redact(value: Any, path: str = "") -> Any:
    """dataclass/dict/list를 재귀적으로 돌며 민감 필드를 치환한 사본을 만듭니다.

    원본 객체는 절대 변경하지 않습니다(항상 새 dict/list를 만들어 반환).
    """
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)

    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else str(k)
            if child_path in _SENSITIVE_DOTTED_PATHS or _is_sensitive_field_name(str(k)):
                out[k] = _REDACTED_PLACEHOLDER
            else:
                out[k] = _redact(v, child_path)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, path) for v in value]
    return value


def redact_settings(settings: Any) -> dict:
    """Settings(또는 그 하위 dataclass)를 민감정보 없이 dict로 변환합니다."""
    return _redact(settings, path="")


def compute_effective_config_hash(settings: Any) -> str:
    """redact_settings() 결과를 결정적으로 직렬화해 SHA-256 해시(hex 16자)를 만듭니다.

    같은 설정이면 항상 같은 해시, 민감정보가 하나라도 다르면(예: 계좌만
    바꾸고 나머지 설정은 동일) 그 필드는 이미 REDACTED 상수로 치환돼
    있으므로 해시에 영향을 주지 않습니다 — "설정이 바뀐 것"과 "계좌만
    바뀐 것"을 이 해시만으로 구분하려는 목적이 아니라, "이 실행이 리뷰
    시점과 동일한 실효 전략/운영 설정으로 떴는가"를 확인하려는
    목적이기 때문에 의도된 동작입니다.
    """
    redacted = redact_settings(settings)
    serialized = json.dumps(redacted, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _run_git(args: list[str], cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # git 바이너리가 없거나(패키징된 배포본 등) 실행 자체가 실패한
        # 경우 — 시작을 절대 막지 않고 조용히 알 수 없음으로 처리.
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def capture_git_info(repo_root: str = ".") -> tuple[str, bool | None]:
    """(git_sha, git_dirty)를 반환합니다. 조회 실패 시 ("unknown", None).

    git_dirty=True는 추적 대상 파일에 커밋되지 않은 변경이 있다는 뜻—
    "지금 이 실행이 리뷰받은 그 커밋 그대로인가"를 나중에 의심할 수 있게
    합니다. git 저장소가 아니거나(패키징 배포본) git이 설치돼 있지 않아도
    프로세스 시작을 절대 막지 않습니다(전부 best-effort).
    """
    sha = _run_git(["rev-parse", "HEAD"], repo_root)
    if sha is None:
        return "unknown", None

    status = _run_git(["status", "--porcelain"], repo_root)
    dirty = None if status is None else bool(status)
    return sha, dirty


# 2026-09-11 (B01 보완, 민우님/GPT 지적): git_sha + git_dirty만으로는
# "이 실행이 정확히 어떤 코드였는지"를 특정할 수 없습니다. 같은 SHA
# 위에 서로 다른 미커밋 패치를 각각 적용한 두 실행은 git_dirty=True로
# 완전히 동일하게 보이므로, "정확한 식별이 가능하다"고 서술하면
# 과장입니다. git_sha+git_dirty는 "리뷰받은 그 커밋과 정확히 같은지"를
# 최소한으로 배제하는 용도(dirty=False면 최소 코드가 커밋 그대로라는
# 뜻)로만 신뢰해야 하고, dirty=True인 두 실행을 구분해야 하는 상황이면
# 이 두 값만으로는 불가능합니다.
#
# 제안(이번 라운드에서 구현하지 않음, 별도 승인 필요): 미커밋 변경의
# 내용까지 구분해야 한다면 `git diff`(추적 대상 파일만)의 해시값을
# "코드 지문(diff fingerprint)"으로 추가 기록하는 방식을 검토할 수
# 있습니다. 원본 diff 텍스트를 그대로 저장하면 (a) 민감정보가 diff에
# 우연히 섞여 들어갈 위험, (b) 로그 파일 크기·보관 정책 문제가 생기므로
# 원문이 아니라 해시만 남기는 것을 전제로 합니다. 이 방식도 "같은
# 패치인지 다른 패치인지"만 구분할 뿐 "패치 내용이 무엇인지"는 여전히
# 알려주지 않으므로, 실제로 필요한 수준(구분만 vs 내용 확인까지)을
# 먼저 정한 뒤 설계해야 합니다 — 이번 제출물에는 포함하지 않습니다.


class RunBaseline:
    """프로세스 한 번의 실행을 식별하는 불변 스냅샷.

    트레이딩 판단에는 전혀 쓰이지 않습니다 — 순수 관측/추적용입니다.
    """

    __slots__ = (
        "run_id", "started_at", "git_sha", "git_dirty",
        "effective_config_hash", "is_mock", "is_paper_trading",
        "python_version",
    )

    def __init__(
        self,
        run_id: str,
        started_at: str,
        git_sha: str,
        git_dirty: bool | None,
        effective_config_hash: str,
        is_mock: bool,
        is_paper_trading: bool,
        python_version: str,
    ) -> None:
        self.run_id = run_id
        self.started_at = started_at
        self.git_sha = git_sha
        self.git_dirty = git_dirty
        self.effective_config_hash = effective_config_hash
        self.is_mock = is_mock
        self.is_paper_trading = is_paper_trading
        self.python_version = python_version

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "git_sha": self.git_sha,
            "git_dirty": "" if self.git_dirty is None else str(self.git_dirty),
            "effective_config_hash": self.effective_config_hash,
            "is_mock": str(self.is_mock),
            "is_paper_trading": str(self.is_paper_trading),
            "python_version": self.python_version,
        }


RUN_BASELINE_FIELDS = [
    "run_id", "started_at", "git_sha", "git_dirty",
    "effective_config_hash", "is_mock", "is_paper_trading", "python_version",
]


def capture_run_baseline(settings: Any, repo_root: str = ".") -> RunBaseline:
    """이번 프로세스 실행의 RunBaseline을 만듭니다(파일 기록은 하지 않음)."""
    git_sha, git_dirty = capture_git_info(repo_root)
    return RunBaseline(
        run_id=str(uuid.uuid4()),
        started_at=now_kst().isoformat(),
        git_sha=git_sha,
        git_dirty=git_dirty,
        effective_config_hash=compute_effective_config_hash(settings),
        is_mock=bool(getattr(settings.broker, "use_mock", False)),
        is_paper_trading=bool(getattr(settings.broker, "is_paper_trading", False)),
        python_version=sys.version.split()[0],
    )


class RunBaselineLogger:
    """run_baseline.csv에 실행마다 한 행씩 추가하는 append-only 로거.

    기존 TRADE_FIELDS/SIGNAL_FIELDS와 완전히 분리된 새 파일이라(이번에
    처음 생기는 파일), 레거시 헤더와의 컬럼 수 불일치 위험이 구조적으로
    없습니다.
    """

    def __init__(self, log_file: str) -> None:
        self.log_file = log_file

    def log(self, baseline: RunBaseline) -> None:
        import csv

        path = Path(self.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = path.exists() and path.stat().st_size > 0

        with path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=RUN_BASELINE_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(baseline.as_dict())


def load_run_baselines(log_file: str) -> list[dict]:
    """run_baseline.csv를 읽어 dict 리스트로 반환합니다(파일 없으면 빈 리스트)."""
    import csv

    path = Path(log_file)
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def parse_run_timestamp(timestamp_iso: str | None) -> datetime | None:
    """ISO8601 문자열을 timezone-aware datetime으로 파싱합니다.

    2026-09-11 (B01 보완, GPT 재현 버그 수정): 기존 구현은 문자열을
    그대로 사전식(lexicographic) 비교했습니다. 이 방식은 한쪽에만
    타임존 오프셋이 붙어 있으면 깨집니다 — 예를 들어 같은 순간이라도
    "2026-09-11T13:00:00"(naive)와 "2026-09-11T13:00:00+09:00"(aware)을
    비교하면, 짧은 쪽이 긴 쪽의 접두사이므로 문자열 비교에서 항상
    "naive < aware"가 되어 실제로는 새 실행이 그 순간 막 시작했어도
    조인에서 제외되고 직전 실행으로 잘못 귀속되는 사례를 재현했습니다.

    이 함수는 대신 실제 datetime 값으로 파싱해 비교합니다. 타임존
    정보가 없는(naive) 입력은 **KST 벽시계 시각이라고 가정**하고
    KST_TZ를 붙입니다 — trades.csv/signal_log.csv/run_baseline.csv에
    실제로 쓰이는 타임스탬프 대다수가 `now_kst().replace(tzinfo=None)`
    또는 `now_kst().isoformat()`(RunBaseline.started_at) 방식이라 이
    가정이 맞기 때문입니다.

    **알려진 한계(이번 수정 범위 밖)**: domain/service/trading_service.py의
    `_write_trade_log()`는 `datetime.now().isoformat()`(시스템 로컬
    시각, 타임존 미지정)을 씁니다. 서버가 UTC로 설정된 환경(AWS 등)이면
    이 KST 가정이 실제로는 틀리고, 이 함수는 그 어긋남을 감지하거나
    보정할 방법이 없습니다 — 이는 trades.csv 자체의 타임스탬프 기록
    방식(로깅 코드)을 고쳐야 하는 별개 문제이며, 이번 B01 조인 로직
    보완과는 분리해 별도로 확인받아야 합니다(이번 제출물에는 포함하지
    않음, 후속 목록에 기록).

    파싱 자체가 실패하면(포맷이 아예 잘못됨) None을 반환합니다 — 실패
    시 크래시하지 않고 "연결 불가"로 안전하게 처리하기 위함입니다.
    """
    if not timestamp_iso:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST_TZ)
    return dt


def resolve_run_id_for_timestamp(baselines: list[dict], timestamp_iso: str) -> str | None:
    """거래/신호 로그의 timestamp에 해당하는 run_id를 시간 범위로 찾습니다.

    "이 timestamp 이전에 시작된 실행 중 가장 늦게 시작된 것"을 그
    timestamp를 만든 실행으로 봅니다(정상 종료 기록이 없어도 재시작
    시각만으로 판정 가능 — 프로세스가 겹쳐 뜰 수 없다는 기존 전제
    (single_instance_lock)와 일치). 해당하는 실행이 없으면(예: 이
    관측 기능 도입 이전의 과거 거래) None을 반환합니다 — 과거 거래를
    엉뚱한 실행에 잘못 연결하지 않기 위해서입니다.

    비교는 parse_run_timestamp()로 실제 datetime 값을 파싱해 수행합니다
    (2026-09-11 B01 보완 — 이전의 문자열 사전식 비교 버그 수정 경위는
    parse_run_timestamp()의 docstring 참고).

    **이 함수의 결과는 "최선 추정"이지 암호학적으로 확정된 연결이
    아닙니다.** run_baseline.csv 기록 자체가 실패한 경우(예: app.log에
    "[RUN_BASELINE] 기록 실패" 경고가 남은 실행)에도 이 함수는 그
    사실을 알 방법이 없습니다 — 그 실행의 시작 기록이 아예 없으므로,
    그 실행이 만든 거래가 조용히 "직전 실행"에 잘못 귀속될 수 있습니다.
    이 함수는 이 실패 모드를 감지하거나 표시할 수 없으므로, 이 결과를
    쓰는 쪽(예: export_daily_bundle.py)이 (a) app.log의
    "[RUN_BASELINE]" 태그로 그날 기록 실패가 있었는지 별도 확인하고,
    (b) 거래/신호의 timestamp가 조인에 쓰인 실행들의 시작 시각보다
    이르면(=애초에 조인 근거가 없으면) "연결 불가"로 명시적으로 구분해
    보여줘야 합니다 — 이 함수가 그런 경우까지 자동으로 판단해 돌려주지
    않습니다.

    입력 파싱에 실패하면(baselines가 비어있거나 timestamp_iso/
    started_at 형식이 잘못됨) 크래시하지 않고 None을 반환합니다
    (fail-closed).
    """
    if not baselines or not timestamp_iso:
        return None

    target_dt = parse_run_timestamp(timestamp_iso)
    if target_dt is None:
        return None

    candidates: list[tuple[datetime, str | None]] = []
    for b in baselines:
        started_dt = parse_run_timestamp(b.get("started_at"))
        if started_dt is None:
            # 파싱 불가능한 행은 조인 후보에서 조용히 제외합니다
            # (크래시하지 않음 — fail-closed).
            continue
        if started_dt <= target_dt:
            candidates.append((started_dt, b.get("run_id")))

    if not candidates:
        return None
    _best_dt, best_run_id = max(candidates, key=lambda pair: pair[0])
    return best_run_id


def perform_run_baseline_startup(
    settings: Any, app_logger: Any, repo_root: str = "."
) -> "RunBaseline | None":
    """앱 시작 시 B01 기록 전체(캡처 + CSV 기록 + 설정 스냅샷)를 수행합니다.

    2026-09-11 (B01 v2 재검토, 민우님/GPT 지적): 원래 이 로직 전체가
    app/main.py 안에 그대로 들어 있었고, save_effective_config_snapshot()
    이 실패해도(디스크 오류 등) None을 반환할 뿐인데 그 반환값을
    확인하지 않아 "설정 스냅샷이 없다"는 사실 자체가 조용히 사라지는
    문제가 있었습니다. 이 함수는 그 반환값을 확인해 실패 시 별도
    [CONFIG_SNAPSHOT_MISSING] 경고를 남기고, app/main.py 밖으로 분리해
    가짜 logger로 직접 단위 테스트할 수 있게 했습니다.

    실패해도(git 없음, 디스크 오류 등) 예외를 절대 전파하지 않습니다 —
    순수 관측 기능이 매매 시작 자체를 막으면 안 되기 때문입니다(기존
    원칙과 동일). 성공하면 RunBaseline을, 최상위 캡처/기록 자체가
    실패하면 None을 반환합니다(설정 스냅샷만 실패한 경우는 RunBaseline을
    반환 — 매매에 필요한 부분은 정상 완료됐기 때문).
    """
    try:
        run_baseline = capture_run_baseline(settings, repo_root=repo_root)
        RunBaselineLogger(settings.storage.run_baseline_log_file).log(run_baseline)

        config_snapshot_dir = str(
            Path(settings.storage.run_baseline_log_file).parent / "run_baseline_configs"
        )
        snapshot_path = save_effective_config_snapshot(run_baseline, settings, config_snapshot_dir)

        app_logger.info(
            f"[RUN_BASELINE] run_id={run_baseline.run_id} | "
            f"git_sha={run_baseline.git_sha} | git_dirty={run_baseline.git_dirty} | "
            f"config_hash={run_baseline.effective_config_hash} | "
            f"mock={run_baseline.is_mock} | paper={run_baseline.is_paper_trading}"
        )
        if snapshot_path is None:
            app_logger.warning(
                f"[CONFIG_SNAPSHOT_MISSING] run_id={run_baseline.run_id} | "
                "설정 스냅샷 저장 실패 — config_hash만으로는 이 실행의 실효 설정을 "
                "복원할 수 없습니다(매매 진행에는 영향 없음)."
            )
        return run_baseline
    except Exception as exc:
        app_logger.warning(f"[RUN_BASELINE] 기록 실패(매매 진행에는 영향 없음): {exc}")
        return None


def save_effective_config_snapshot(
    baseline: "RunBaseline", settings: Any, snapshot_dir: str
) -> Path | None:
    """이번 실행의 redacted 설정 전체를 run_id별 JSON 파일로 저장합니다.

    2026-09-11 (B01 보완, 민우님/GPT 지적): effective_config_hash 하나만
    으로는 "그때 정확히 어떤 설정값이었는지"를 복원할 수 없습니다(해시는
    되돌릴 수 없으므로). 나중에 "이 실행이 리뷰받은 설정과 실제로
    같았는가"를 확인하려면 해시 비교만으로는 부족하고 설정 원문(민감
    정보 제외)이 필요합니다 — 이 함수가 그 전체 스냅샷을 남깁니다.

    run_baseline.csv(고정 컬럼의 append-only CSV)에는 넣지 않고 별도
    JSON 파일로 분리합니다 — 설정 스키마는 CSV 헤더처럼 고정된 컬럼
    수를 가정할 수 없고(중첩 dict, 향후 필드 추가 등) CSV 한 셀에
    직렬화하면 가독성도 떨어지기 때문입니다.

    다른 B01 함수와 동일한 원칙: 실패해도(디스크 쓰기 오류 등) 예외를
    전파하지 않고 None을 반환할 뿐입니다 — 순수 관측 기능이 매매 시작을
    막으면 안 됩니다.
    """
    try:
        path = Path(snapshot_dir) / f"{baseline.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": baseline.run_id,
            "started_at": baseline.started_at,
            "effective_config_hash": baseline.effective_config_hash,
            "config": redact_settings(settings),
        }
        path.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path
    except OSError:
        return None
