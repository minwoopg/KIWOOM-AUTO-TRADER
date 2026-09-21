from __future__ import annotations

"""우선순위1 1차: 체결조회 증거 독립 저장 + 커버리지 계측.

**이 모듈이 하는 일은 정확히 이것뿐입니다**: `Broker.get_order_status_evidence()`
호출 결과(성공이든 실패든)를 매매 루프를 절대 지연시키지 않는 방식으로
독립된 append-only 로그에 남기고, 그 로그로부터 "고유 주문 기준" 커버리지
지표를 계산합니다.

**이 모듈이 하지 않는 일**: 손익 계산, FIFO 매칭, 확정 손익 표시,
RiskManager/DailyReporter 연결, 조회 빈도 확대. `TrackedOrderJournalStore`도
전혀 재사용하지 않습니다(그 저장소는 안전한 시점에 삭제되므로 영구
증거 보존에 부적합 — 2026-09-18 지적 반영).

설계 배경(민우님 GPT 기반 review, 2026-09-18):
1. `derive_broker_order_status()`의 FILLED 확정은 가격 유효성을
   전혀 보장하지 않으므로, 이 모듈은 "판정"과 "가격 원문"을 분리해
   기록합니다 — `OrderStatusObservation.filled_price_parsed`는 공식
   판정값 그대로이고, `cntr_matches`/`oso_matches`의 각 원소가 담는
   `cntr_pric_raw` 등은 판정과 무관하게 원문을 그대로 보존합니다.
2. 커버리지는 조회 횟수가 아니라 **고유 주문 수** 기준입니다
   (`compute_coverage()` 참고) — 같은 주문을 반복 조회해도 분자가
   1건으로만 잡힙니다.
3. 디스크 쓰기는 메인 폴링 스레드에서 절대 기다리지 않습니다
   (`OrderStatusObservationRecorder`: 제한된 큐 + 별도 기록 스레드).
   "매매 루프에 영향이 없다"가 아니라 "매매 루프가 디스크 완료를
   기다리지 않는다"로 주장 범위를 좁힙니다.
4. 강제 종료 시 유실 구간을 정확히 복원할 수 있다고 약속하지
   않습니다 — `restart_id`+단조증가 `seq_no`+`clean_shutdown` 마커로
   "확인 가능한 범위"와 "불확실 구간"만 구분합니다.
5. `account_scope_id`가 설정되지 않아도 프로그램 기동을 막지
   않습니다 — 이 경우 관측 기능 자체가 비활성화되고, 커버리지는
   "0%"가 아니라 별도의 "계측 비활성" 상태로 표시됩니다.
"""

import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path

from infra.broker.kiwoom_order_status import normalize_order_id
from typing import Any, Literal

SCHEMA_VERSION = 1

# ── 환경 3분류 ────────────────────────────────────────────────────
# 2026-09-18 (지적 반영): base_url만으로는 로컬 MockBroker 실행까지
# 구분하지 못합니다. app/main.py의 build_broker()가 이미
# settings.broker.use_mock으로 MockBroker/KiwoomBroker를 나누고,
# KiwoomBroker 내부에서 base_url로 모의투자/실전투자를 나누므로
# 그 두 판단을 그대로 재사용합니다(새 판정 로직을 만들지 않음).
ENV_LOCAL_MOCK = "local_mock"
ENV_KIWOOM_MOCK = "kiwoom_mock"
ENV_KIWOOM_REAL = "kiwoom_real"


def resolve_env(*, use_mock: bool, is_paper_trading: bool) -> str:
    """`BrokerConfig`의 기존 두 필드만으로 환경 3분류를 판정합니다.

    2026-09-18: 처음에는 `base_url` 문자열에 "mock"이 포함되는지로
    판정하려 했으나, `infra/storage/run_baseline.py`가 이미 프로세스
    시작 시점에 `is_mock`/`is_paper_trading`을 명시적 bool로 확정해
    기록하고 있음을 확인해 그 값을 그대로 재사용합니다(문자열
    추정보다 신뢰할 수 있음, `resolve_env_from_baseline_row()`와
    동일한 판정 기준으로 일관성 유지).
    """

    if use_mock:
        return ENV_LOCAL_MOCK
    return ENV_KIWOOM_MOCK if is_paper_trading else ENV_KIWOOM_REAL


# ── 관측 레코드 ───────────────────────────────────────────────────

QueryOutcome = Literal["success", "api_error", "partial"]


@dataclass
class OrderStatusObservation:
    """`get_order_status_evidence()` 호출 하나(성공/실패 모두)에 대한
    관측 레코드. 판단·집계를 하지 않고 원문을 그대로 담습니다.

    2026-09-18 (지적 2번 반영): `query_id`는 호출 **이전에** 발급해,
    실패한 조회도 같은 식별자로 종료 결과를 기록할 수 있게 합니다
    (성공한 조회만 기록되던 v2의 결함을 수정).
    """

    query_id: str
    started_at: str          # 호출 직전 시각(ISO) — 성공/실패 모두 존재
    finished_at: str | None  # 호출 완료 시각(ISO) — 실패 시에도 채움
    account_scope_id: str
    env: str
    symbol: str
    requested_order_id: str
    side_context: str            # "BUY_PENDING" | "SELL_PENDING" | "ORPHAN"
    query_kind: str              # 위와 동일한 값(호출부 표현 그대로 보존)
    pending_age_sec_at_query: float

    outcome: QueryOutcome        # "success" | "api_error" | "partial"
    failure_stage: str | None = None   # 실패/부분 성공 시: "oso_fetch" | "cntr_fetch" | "evidence_build" 등
    error_repr: str | None = None      # 예외 요약(민감정보 없음 전제 — 저장 전 export 단계에서 추가 마스킹)

    psm_broker_status: str | None = None   # BrokerOrderStatus 값 — PSM이 실제 소비한 값과 동일(감사용)
    filled_price_parsed: int | None = None  # 공식 판정과 결합된 값(=BrokerOrder.filled_price)

    # 각 원소 예: {"response_order_id", "ord_stt", "cntr_pric_raw", "cntr_qty_raw",
    #             "oso_qty_raw", "ord_qty_raw", "io_tp_nm", "trde_tp", "raw": {...}}
    # "raw"에는 위에 구조화되지 않은 나머지 원문 필드(체결시각/체결식별자 등,
    # 실측 전이라 키 이름을 아직 확정할 수 없음)를 통째로 보존합니다.
    # 제외하는 필드: 없음 — export 단계의 SENSITIVE_KEYS 마스킹만 적용됩니다.
    cntr_matches: list = field(default_factory=list)
    oso_matches: list = field(default_factory=list)
    cntr_match_count: int = 0
    oso_match_count: int = 0

    base_quantity_before_order: int | None = None
    target_quantity_after_order: int | None = None
    journal_linked: bool = False   # TrackedOrderRecord와 연결해 스냅샷을 채울 수 있었는가

    # 2026-09-18 재검토 반영(지적 3번): 이 주문이 실제로 접수된 시각
    # (TrackedOrderRecord.accepted_at, ISO 문자열) — journal_linked가
    # True일 때만 채워집니다. exporter의 order_date 판정은 "오늘
    # trades.csv에 같은 order_id가 있으면 오늘 주문"이라는 추정
    # (키움 주문번호가 날짜마다 재사용될 수 있어 오판 위험이 있음)
    # 대신 이 값을 우선 근거로 씁니다 — 이 값이 없으면 "미확인"으로
    # 남기고 추정하지 않습니다.
    order_accepted_at: str | None = None

    write_id: str = ""       # 이 "사건"의 식별자 — 재시도는 같은 값 재사용, 새 조회만 새로 발급
    restart_id: str = ""
    seq_no: int = 0

    schema_version: int = SCHEMA_VERSION

    def content_hash(self) -> str:
        """`write_id`가 같은 두 레코드의 내용이 실제로 동일한지 비교하기
        위한 해시(write_id 자체와 순전히 기록 시점에만 달라질 수 있는
        필드는 제외 — finished_at 같은 시각 필드는 재시도마다 달라질
        수 있어 해시에서 뺍니다)."""

        payload = asdict(self)
        for k in ("finished_at", "seq_no"):
            payload.pop(k, None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


QUARANTINE_SUFFIX_PREFIX = ".unrecoverable-"


def find_quarantined_observation_files(observation_log_path: str | Path) -> list[Path]:
    """`_quarantine_and_rotate_file()`이 옆으로 치워둔 손상 파일들을 찾습니다
    (2026-09-21 3차 재검토 반영, 지적 1번: "격리 파일의 정상 기록이
    집계·번들에서 빠짐").

    격리 시점 이전까지 그 파일에 쌓여 있던 정상 레코드는 삭제되지
    않고 이 파일들 안에 그대로 남아있는데, exporter가 지금까지
    `observation_log_path`(격리 이후 새로 시작된 파일)만 읽어서 그
    정상 레코드들이 이후 어떤 집계·번들에도 다시 나타나지 않았습니다
    (재현된 버그). 기록기(쓰기 쪽)와 exporter(읽기 쪽)가 같은
    접미사 규칙(`QUARANTINE_SUFFIX_PREFIX`)을 공유해야 하므로, 이
    함수를 여기 두고 양쪽 모두 재사용합니다(`status_path_for()`와
    동일한 이유).

    반환은 파일명(=격리 시각 문자열 포함) 오름차순 — 격리된 순서와
    일치합니다.
    """
    p = Path(observation_log_path)
    parent = p.parent
    if not parent.exists():
        return []
    prefix = p.name + QUARANTINE_SUFFIX_PREFIX
    return sorted(
        f for f in parent.iterdir() if f.is_file() and f.name.startswith(prefix)
    )


def status_path_for(observation_log_path: str | Path) -> Path:
    """관측 로그 파일 경로로부터 "실행 중 상태 스냅샷" 파일 경로를
    유도합니다(2026-09-18 재재검토 반영, 지적 5번).

    기록기(쓰기 쪽, 이 모듈)와 exporter(읽기 쪽,
    `export_daily_bundle.py`)가 각자 다른 규칙으로 경로를 계산하면
    서로 어긋날 위험이 있으므로, 두 쪽 모두 이 함수 하나만 공유해
    항상 같은 결과를 내도록 합니다.
    """
    p = Path(observation_log_path)
    return p.with_name(p.stem + ".status.json")


def build_entry_evidence(raw: dict) -> dict:
    """cntr/oso 원본 행 하나를 관측 레코드용 구조로 변환합니다.

    2026-09-18 (지적 2번 반영): `ord_qty`(요청 수량 원문)를 누락하지
    않고 포함합니다 — 기존 FILLED 판정 자체가 이 값을 쓰므로, 나중에
    수량 관계(요청/체결/미체결)를 검증하려면 반드시 필요합니다.
    구조화되지 않은 나머지 필드는 "raw"에 원문 그대로 보존합니다
    (필드 목록이 실측 전이라 어떤 키가 더 있을지 확정할 수 없으므로,
    선별 보존이 아니라 전체 보존 + 구조화된 상위 필드로 접근성만
    높이는 방식을 택함).
    """

    return {
        "response_order_id": raw.get("ord_no"),
        "ord_stt": raw.get("ord_stt"),
        "ord_qty_raw": raw.get("ord_qty"),
        "cntr_qty_raw": raw.get("cntr_qty"),
        "oso_qty_raw": raw.get("oso_qty"),
        "cntr_pric_raw": raw.get("cntr_pric"),
        "io_tp_nm": raw.get("io_tp_nm"),
        "trde_tp": raw.get("trde_tp"),
        "raw": dict(raw),
    }


# ── 큐 + 별도 기록 작업자 ─────────────────────────────────────────

class OrderStatusObservationRecorder:
    """제한된 큐에 넣고 별도 스레드가 디스크에 append합니다.

    2026-09-18 (지적 3/4번 반영):
    - `record()`는 큐가 가득 차거나 예외가 나도 절대 블로킹하지
      않습니다 — 그 자리에서 `dropped_count`만 올리고 즉시 반환합니다.
      "작업자 스레드가 매매 루프에 전혀 영향을 주지 않는다"는 표현
      대신 "매매 루프가 디스크 완료를 기다리지 않는다"로 범위를
      좁힙니다(GIL/스케줄링 수준의 미세한 영향까지 없다고 주장하지
      않음).
    - 쓰기 실패는 **같은 파일에 재시도하지 않습니다** — 부분 쓰기가
      파일 중간에 손상된 행으로 남을 위험을 피하기 위해, 실패한
      레코드는 버리고 `lost_count`만 올립니다. 호출부가 정말 같은
      사건을 다시 기록하고 싶다면 같은 `write_id`로 새 `record()`
      호출을 큐에 다시 넣어야 하며, 그 경우 새 줄로 파일 끝에
      추가됩니다(파일 중간이 아니라 항상 끝에만 추가되므로, export
      단계의 "마지막 줄만 불완전할 수 있다"는 가정이 깨지지 않습니다).
    - `shutdown()`은 상한 시간 안에 큐를 드레인하고, 그 안에 못 끝내면
      나머지는 유실로 간주해 프로세스 종료 자체를 막지 않습니다.
      `clean_shutdown`(정상 종료 경로 도달+제한시간 내 드레인 완료),
      `queue_drained`(드레인 시도가 끝났는지 — 디스크 성공을 의미하지
      않음), `dropped_count`(큐 포화/쓰기 실패로 버려진 건수)를
      서로 다른 사실로 분리해 마커에 남깁니다.
    """

    def __init__(
        self,
        path: str,
        *,
        app_logger: logging.Logger | None = None,
        maxsize: int = 200,
        shutdown_drain_timeout_sec: float = 3.0,
        status_update_interval_sec: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._app_logger = app_logger or logging.getLogger(__name__)
        # 2026-09-18 재재검토 반영(지적 1번): truncate 자체가 실패해
        # "쓰기 실패 이전" 경계로도 되돌릴 수 없게 되면, 그 사실이
        # 확인될 때까지는 이 파일에 더 이상 쓰지 않습니다 — 그래야
        # 다음 정상 기록이 이미 손상된 바이트 뒤에 이어붙어 함께
        # 손상되는 연쇄를 막을 수 있습니다.
        self._file_healthy = True
        # 2026-09-18 재재검토 반영(지적 5번): 종료 시에만 남는 마커와
        # 별도로, 기록기가 살아있는 동안 주기적으로 갱신하는 "현재
        # 상태" 스냅샷 파일 경로 — exporter가 "실행 중"인지 판단할 때
        # 이 파일의 최근 갱신 시각을 씁니다.
        self._status_path = status_path_for(self.path)
        self._status_update_interval_sec = status_update_interval_sec
        self._last_status_update_monotonic = 0.0
        self._queue: "queue.Queue[OrderStatusObservation]" = queue.Queue(maxsize=maxsize)
        self._shutdown_drain_timeout_sec = shutdown_drain_timeout_sec
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._seq_lock = threading.Lock()
        self._seq_no = 0
        self.restart_id = uuid.uuid4().hex
        self.dropped_count = 0
        self._dropped_lock = threading.Lock()
        # 2026-09-18 재검토 반영(지적 1번): fsync 실패는 dropped_count와
        # 별도로 셉니다 — write() 자체는 성공해 파일에 온전한 줄이
        # 남았고, 단지 디스크 반영이 확인 안 됐을 뿐이라 "유실"과는
        # 다른 사실이기 때문입니다.
        self.fsync_unconfirmed_count = 0
        # 2026-09-18 재검토 반영(지적 2번, 재현된 버그): 큐 포화로 인한
        # 유실은 dropped_count(전체 유실)와 별도로도 세어둡니다 —
        # record()는 더 이상 이 값이 바뀌었다고 직접 로그를 남기지
        # 않고(매매 스레드가 로그 I/O를 기다리는 걸 막기 위함),
        # 작업자 스레드가 폴링마다 이 값의 변화를 감지해 대신 로그를
        # 남깁니다(`_maybe_log_queue_full_change()` 참고).
        self._queue_full_dropped_count = 0
        self._last_logged_queue_full_count = 0
        # shutdown()이 join(timeout=...)에서 이미 반환된 뒤에도 작업자
        # 스레드가 실제로 마커를 남겼는지(성공/실패)를 나중에 확인할 수
        # 있게 기록해 둡니다 — shutdown() 자신은 이 값을 기다리지
        # 않습니다(제한시간 안에서만 join).
        self.marker_written: bool | None = None

    # ── 시작/복구 ──
    def start(self) -> None:
        self._quarantine_incomplete_tail()
        # 2026-09-18 재재검토 반영(지적 5번): 상태 스냅샷 파일을 기록
        # 시작 즉시 만들어 둡니다 — 그래야 exporter가 "상태 파일이
        # 아예 없음(계측 비활성/시작 전)"과 "있지만 오래됨(비정상
        # 종료 가능성)"을 구분할 수 있습니다.
        self._write_running_status()
        self._last_status_update_monotonic = time.monotonic()
        self._thread = threading.Thread(
            target=self._writer_loop, name="order-status-observation-writer", daemon=True,
        )
        self._thread.start()

    def _quarantine_incomplete_tail(self) -> None:
        """이전 실행이 강제 종료돼 마지막 줄이 잘려있을 수 있는 경우를
        대비합니다. 마지막 줄만 JSON 파싱에 실패하면 그 줄만
        `<path>.corrupt`로 옮기고 CRITICAL 로그를 남깁니다 — 다음
        기록이 손상된 줄 뒤에 이어붙어 두 레코드가 한 줄로 뭉개지는
        것을 막기 위함입니다. 마지막 줄이 아닌 곳에서 손상이
        발견되면(파일 자체 손상 가능성) 프로젝트의 fail-close 원칙대로
        CRITICAL만 남기고 계속 진행합니다(관측 기능이므로 매매를
        막지는 않음 — 그러나 조용히 넘어가지 않음).
        """

        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 읽기 실패 — {type(exc).__name__}: {exc}"
            )
            return
        if lines:
            bad_indices = []
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    json.loads(stripped)
                except json.JSONDecodeError:
                    bad_indices.append(i)
            if bad_indices:
                last_idx = len(lines) - 1
                if bad_indices == [last_idx]:
                    # 마지막 줄만 손상 — 정상적인 강제종료 시나리오로 취급
                    corrupt_path = self.path.with_suffix(self.path.suffix + ".corrupt")
                    with corrupt_path.open("a", encoding="utf-8") as f:
                        f.write(
                            f"# quarantined_at={datetime.now().isoformat()} restart_id={self.restart_id}\n"
                        )
                        f.write(lines[last_idx])
                    remaining = "".join(lines[:last_idx])
                    tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                    tmp.write_text(remaining, encoding="utf-8")
                    os.replace(tmp, self.path)
                    self._app_logger.critical(
                        f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 마지막 줄이 불완전해 "
                        f"{corrupt_path.name}으로 격리했습니다(강제종료 추정) — 나머지는 정상 유지"
                    )
                else:
                    self._app_logger.critical(
                        f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 마지막 줄이 아닌 위치({bad_indices})에서 "
                        f"손상 발견 — 파일 자체가 손상됐을 수 있습니다. 자동 복구하지 않고 그대로 둡니다."
                    )
        # 2026-09-18 재재검토 반영(지적 1번, 재현된 버그): 마지막 줄이
        # 완전한 JSON이어도 파일 자체가 개행으로 끝나지 않는 경우가
        # 있습니다(예: 이전 실행이 write()+flush()까지는 성공했지만
        # 그 뒤 "\n" 한 글자를 마저 쓰기 직전에 강제 종료된 경우 —
        # 위 bad_indices 검사는 각 줄을 "\n" 기준으로 나눈 뒤 개별
        # json.loads()만 확인하므로 이 사례를 잡지 못합니다). 이 상태를
        # 그대로 두면 다음 기록이 이 줄 끝에 바로 이어붙어 **두 레코드
        # 모두 파싱 불가능**해집니다. 내용 손실 없이 개행만 보정합니다.
        self._ensure_trailing_newline()

    def _ensure_trailing_newline(self) -> None:
        """파일이 존재하고 비어있지 않은데 마지막 바이트가 개행이
        아니면 개행 한 글자만 덧붙입니다(2026-09-18 재재검토 반영,
        지적 1번). 내용은 전혀 바꾸지 않으므로 안전합니다."""
        if not self.path.exists():
            return
        try:
            with self.path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return
                f.seek(-1, os.SEEK_END)
                last_byte = f.read(1)
            if last_byte != b"\n":
                with self.path.open("ab") as f:
                    f.write(b"\n")
                self._app_logger.warning(
                    f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 마지막 줄이 개행 없이 끝나"
                    " 있어 보정했습니다(내용 손실 없음 — 이전 실행이 마지막 개행 쓰기"
                    " 직전에 종료된 것으로 추정, 보정하지 않으면 다음 기록이 이어붙어"
                    " 두 레코드 모두 파싱 불가능해짐)"
                )
        except OSError as exc:
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 개행 보정 확인 실패 — "
                f"{type(exc).__name__}: {exc} — 다음 기록이 이 파일 끝에 손상 없이"
                " 이어붙는다는 보장이 없습니다"
            )
            self._file_healthy = False

    # ── 기록 ──
    def record(self, observation: OrderStatusObservation) -> None:
        with self._seq_lock:
            self._seq_no += 1
            observation.seq_no = self._seq_no
        observation.restart_id = self.restart_id
        try:
            self._queue.put_nowait(observation)
        except queue.Full:
            # 2026-09-18 재검토 반영(지적 2번, 재현된 버그): 예전엔 여기서
            # app_logger.warning()을 **동기** 호출했습니다 — 느린 로그
            # 핸들러를 주입해 측정하니 이 record()를 호출한 매매 스레드
            # 자체가 ~0.20초 대기했습니다. record()는 "매매 루프가 디스크/
            # 로그 완료를 기다리지 않는다"가 핵심 계약이므로, 여기서는
            # 카운터만 올리고 즉시 반환합니다 — 실제 로그는 작업자
            # 스레드가 폴링 중 이 값의 변화를 감지해 대신 남깁니다
            # (`_maybe_log_queue_full_change()`).
            with self._dropped_lock:
                self.dropped_count += 1
                self._queue_full_dropped_count += 1

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                observation = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._maybe_log_queue_full_change()
                self._maybe_update_running_status()
                continue
            self._write_one(observation)
            self._maybe_log_queue_full_change()
            self._maybe_update_running_status()
        # 2026-09-18 재검토 반영(지적 2번, 재현된 버그): 종료 마커 기록도
        # 이 작업자 스레드 안에서 수행합니다 — 예전엔 shutdown()을 호출한
        # (매매) 스레드가 join(timeout=...) 이후 **직접** 마커 파일을
        # 썼는데, 이 쓰기 자체는 그 timeout의 적용을 받지 않았습니다
        # (재현: timeout=0.01초로 설정해도 실제 마커 쓰기에 ~0.20초가
        # 걸림 — 디스크가 멈추면 종료시간 상한이 전혀 보장되지 않음).
        # 이제 마커 쓰기가 이 스레드 안에서 이뤄지므로, 호출 스레드는
        # join(timeout=...) 하나로만 전체 대기 시간이 제한됩니다 — 마커
        # 쓰기가 느려지거나 멈춰도 join이 timeout에 도달하면 호출
        # 스레드는 이미 반환된 뒤이고, 이 스레드는 계속 남아 마커
        # 쓰기를 끝내려 시도합니다(데몬 스레드라 프로세스 종료를 막지
        # 않음).
        self._write_shutdown_marker()
        self._write_running_status(clean_shutdown=self.marker_written)

    def _maybe_update_running_status(self) -> None:
        """상태 스냅샷 파일을 너무 잦지 않게(기본 5초 간격) 갱신합니다
        (2026-09-18 재재검토 반영, 지적 5번) — exporter가 이 파일의
        `updated_at`이 최근인지로 "지금 실행 중"을 판단하므로, 매매
        활동이 없는 구간에도(관측할 조회 자체가 없어도) 폴링 주기마다
        살아있다는 신호를 남겨야 합니다."""
        now_mono = time.monotonic()
        if now_mono - self._last_status_update_monotonic >= self._status_update_interval_sec:
            self._write_running_status()
            self._last_status_update_monotonic = now_mono

    def _write_running_status(self, *, clean_shutdown: bool | None = None) -> None:
        """"지금 이 순간의" 저장 품질 상태를 별도 스냅샷 파일에
        남깁니다(2026-09-18 재재검토 반영, 지적 5번).

        기존 종료 마커는 실제로 종료 배선을 탄 시점에만 남으므로,
        번들이 보통 그렇듯 프로그램이 아직 실행 중일 때 생성되면
        "이 실행의" 마커는 존재할 수 없습니다 — exporter가 이 사실을
        "관측 실패"로 오인하지 않으려면, 실행 중에도 주기적으로
        갱신되는 별도의 상태 신호가 필요합니다. `clean_shutdown`은
        아직 종료하지 않았으면(정상 동작 중) `None`으로 남겨 "실행
        중이거나 확인 불가"를 뜻하고, 종료 배선을 탄 뒤에만 실제
        마커 기록 성공 여부(`marker_written`)로 True/False가 채워집니다
        — 그래야 exporter가 "정상 종료/종료 확인 불가/실행 중"을
        이 파일 하나만으로 구분할 수 있습니다.

        이 파일 쓰기 자체가 실패해도(디스크 문제 등) 관측/매매에
        영향을 주지 않도록 예외를 삼키고 경고만 남깁니다 — 진단
        정보 하나가 없어지는 것일 뿐, 핵심 기능이 아니기 때문입니다.
        """
        with self._dropped_lock:
            dropped_count = self.dropped_count
            fsync_unconfirmed_count = self.fsync_unconfirmed_count
            queue_full_dropped_count = self._queue_full_dropped_count
        payload = {
            "restart_id": self.restart_id,
            "updated_at": datetime.now().isoformat(),
            "dropped_count": dropped_count,
            "fsync_unconfirmed_count": fsync_unconfirmed_count,
            "queue_full_dropped_count": queue_full_dropped_count,
            "file_healthy": self._file_healthy,
            "clean_shutdown": clean_shutdown,
        }
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._status_path)
        except OSError as exc:
            self._app_logger.warning(
                f"[ORDER_STATUS_OBS_RECOVERY] 실행 상태 스냅샷({self._status_path}) 쓰기 실패"
                f"(진단 정보만 영향 — 관측/매매 자체는 계속됨) — {type(exc).__name__}: {exc}"
            )

    def _maybe_log_queue_full_change(self) -> None:
        with self._dropped_lock:
            current = self._queue_full_dropped_count
        if current != self._last_logged_queue_full_count:
            self._app_logger.warning(
                f"[ORDER_STATUS_OBS_QUEUE_FULL] 누적 큐 포화 유실 {current}건 — "
                f"작업자 스레드에서 비동기로 기록(매매 루프는 대기하지 않았음)"
            )
            self._last_logged_queue_full_count = current

    def _write_one(self, observation: OrderStatusObservation) -> None:
        """한 레코드를 파일 끝에 append합니다.

        2026-09-18 재검토 반영(지적 1번, 재현된 버그): 쓰기 도중 예외가
        나면 이미 파일에 일부 바이트가 써졌을 수 있습니다(레코드 하나가
        부분적으로만 기록됨). 예전엔 이 상태를 그대로 두고 다음
        레코드를 append했는데, 그러면 부분 기록된 바이트 뒤에 다음
        레코드가 바로 이어붙어 **두 레코드가 한 줄로 뭉개져 함께 파싱
        불가능**해졌습니다(재현: w1 부분 쓰기 실패 → w2 정상 기록
        시도 → 실제로는 w1+w2 모두 복구 불가). 이제 쓰기 시도 "직전"
        파일 크기를 먼저 확인해 두고, 쓰기 중 예외가 나면 그 크기로
        파일을 truncate해 부분 바이트를 제거한 뒤에만 다음 레코드를
        받습니다 — 그래서 파일은 항상 "완전한 줄들"로만 끝나는
        상태를 유지합니다.

        fsync() 실패는 별도로 다룹니다 — write()/flush() 자체는 이미
        성공해 파일에 완전한 줄이 남았고, 단지 OS 캐시에서 디스크로의
        반영이 확인되지 않았을 뿐이므로 "쓰기 실패(내용 유실)"와는
        다른 성격입니다. 조용히 무시하지 않고 별도 카운터
        (`fsync_unconfirmed_count`)와 로그 태그
        (`[ORDER_STATUS_OBS_FSYNC_FAILED]`)로 "저장 내구성 미확인"
        상태를 남기되, 레코드 자체는 유실 처리하지 않습니다.

        2026-09-18 재재검토 반영(지적 1번, 재현된 버그): truncate 복구
        **자체가 실패**하면(디스크 문제 등으로 되돌릴 크기조차 확정할
        수 없으면) 예전엔 그냥 다음 레코드를 이 파일에 계속
        append했습니다 — 그러면 그 다음 레코드도 이미 손상된 바이트
        뒤에 이어붙어 함께 손상됐습니다(재현: truncate 실패 → w2도
        파싱 불가). 이제 truncate 자체가 실패하면(또는 truncate
        대상 크기조차 확인할 수 없으면) 이 파일을 더 이상 믿지 않고
        `_quarantine_and_rotate_file()`로 옆으로 치운 뒤 같은 경로에
        새 빈 파일로 다시 시작합니다 — 그래서 그 다음 레코드부터는
        항상 깨끗한 파일에 쓰이고, `dropped_count`는 실제로 복구
        불가능해진 레코드 수와 계속 일치합니다(격리된 파일에 추가로
        덧붙는 레코드가 없으므로).
        """
        if not self._file_healthy:
            with self._dropped_lock:
                self.dropped_count += 1
            return

        try:
            pre_size = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            # 크기 확인 자체가 안 되면 truncate 대상에서 제외합니다 —
            # 잘못된 크기로 truncate해 더 큰 손상을 만들지 않기 위함.
            pre_size = None

        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(observation.to_json_line() + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as fsync_exc:
                    with self._dropped_lock:
                        self.fsync_unconfirmed_count += 1
                    self._app_logger.warning(
                        f"[ORDER_STATUS_OBS_FSYNC_FAILED] write_id={observation.write_id} — "
                        f"{type(fsync_exc).__name__}: {fsync_exc} — write()는 성공했으나 디스크"
                        f" 반영 확인 안 됨(레코드는 유실 처리하지 않음, 누적 미확인 "
                        f"{self.fsync_unconfirmed_count}건)"
                    )
        except Exception as exc:
            with self._dropped_lock:
                self.dropped_count += 1
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_WRITE_FAILED] write_id={observation.write_id} — "
                f"{type(exc).__name__}: {exc} — 같은 파일에 재시도하지 않고 유실 처리"
            )
            recovered = self._truncate_partial_write(pre_size) if pre_size is not None else False
            if not recovered:
                self._quarantine_and_rotate_file()

    def _truncate_partial_write(self, pre_size: int) -> bool:
        """쓰기 실패 후 파일을 실패 이전 크기로 되돌려 부분 쓰기 바이트를
        제거합니다 — 다음 레코드가 항상 깨끗한 줄 경계 뒤에 이어붙도록
        보장합니다(2026-09-18 재검토 지적 1번). 성공하면 True, 실패하면
        False를 반환합니다(2026-09-18 재재검토 반영 — 호출부가 실패
        시 이 파일에 더 이상 쓰지 않고 격리/전환하도록 신호를 줌)."""
        try:
            with self.path.open("r+b") as f:
                f.truncate(pre_size)
            return True
        except OSError as exc:
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_WRITE_FAILED] 부분 쓰기 복구(truncate) 실패 — "
                f"{type(exc).__name__}: {exc} — 파일이 부분 쓰기 상태로 남아있을 수 있음"
            )
            return False

    def _quarantine_and_rotate_file(self) -> None:
        """truncate 복구까지 실패해 파일의 마지막 경계를 더 이상 신뢰할
        수 없을 때, 이 파일에는 더 이상 쓰지 않고 같은 경로에 새 빈
        파일로 전환합니다(2026-09-18 재재검토 반영, 지적 1번 — "정상
        경계 복구를 확인하지 못한 파일에는 추가 기록을 중단하고, 새
        파일로 전환하거나 기록기를 오류 상태로 전환해야 합니다").

        기존 파일은 삭제하지 않고 타임스탬프가 붙은 이름으로 옆에
        남겨둡니다(수동 복구/조사 가능하도록). 격리(이동) 자체가
        실패하면(디스크 문제가 더 근본적인 경우) 이 파일을 계속
        믿을 수 없으므로 `_file_healthy`를 False로 유지해 이후
        `_write_one()`이 즉시 유실 처리하고 반환하게 합니다 — 매매
        프로그램 자체를 멈추지는 않지만, 손상 위에 계속 덮어쓰는
        것만은 막습니다.
        """
        old_path = self.path
        suffix = (
            f"{QUARANTINE_SUFFIX_PREFIX}{datetime.now().strftime('%Y%m%d%H%M%S')}"
            f"-{uuid.uuid4().hex[:8]}"
        )
        quarantined = old_path.with_name(old_path.name + suffix)
        try:
            os.replace(old_path, quarantined)
        except OSError as exc:
            self._file_healthy = False
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_RECOVERY] {old_path} 손상 파일 격리 실패 — 이 기록기는"
                f" 앞으로 이 파일에 기록하지 않습니다(손상 위에 계속 덮어쓰는 것을 막기"
                f" 위함, dropped_count로 계속 유실 집계됨) — {type(exc).__name__}: {exc}"
            )
            return
        self._file_healthy = True
        self._app_logger.critical(
            f"[ORDER_STATUS_OBS_RECOVERY] {old_path.name}의 마지막 경계를 복구하지 못해"
            f" {quarantined.name}으로 격리하고 같은 경로에 새 파일로 전환했습니다"
            f"(복구 불가능한 손상 확인 — 이후 정상 기록은 새 파일에 쌓입니다)"
        )

    # ── 종료 ──
    def _write_shutdown_marker(self) -> None:
        """종료 마커를 파일에 append합니다(작업자 스레드 안에서만 호출됨).

        2026-09-18 재검토 반영(지적 2번): shutdown()을 호출한 스레드가
        아니라 이 작업자 스레드 자신이 마커를 씁니다 — 그래서 이 쓰기가
        아무리 느려지거나 멈춰도 shutdown()의 join(timeout=...)이 이미
        정한 상한 시간을 넘기지 않습니다.
        """
        result = {
            "clean_shutdown": True,
            "queue_drained": True,
            "dropped_count": self.dropped_count,
            "fsync_unconfirmed_count": self.fsync_unconfirmed_count,
            "restart_id": self.restart_id,
            "last_seq_no": self._seq_no,
            "shutdown_at": datetime.now().isoformat(),
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"__marker__": "shutdown", **result}, ensure_ascii=False) + "\n")
            self.marker_written = True
        except Exception as exc:
            self.marker_written = False
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_SHUTDOWN_MARKER_FAILED] {type(exc).__name__}: {exc}"
            )

    def shutdown(self) -> dict:
        """상한 시간 안에 드레인을 기다리기만 합니다 — 그 이상의 블로킹
        I/O(마커 파일 쓰기 등)는 이 메서드 자신이 하지 않습니다
        (2026-09-18 재검토 반영, 지적 2번의 재현된 버그: 예전엔 join
        이후 이 메서드가 직접 마커를 써서, join의 timeout이 종료
        시간의 실제 상한이 되지 못했습니다).

        반환값의 `clean_shutdown`은 "제한시간 내에 작업자 스레드가
        (큐 드레인 + 마커 기록까지) 완전히 끝났다"는 뜻이고,
        `queue_drained`는 "제한시간 내에 스레드가 살아있는 상태를
        벗어났다"는 뜻입니다 — 스레드가 아직 살아있으면(마커 쓰기가
        느려서 등) 마커가 실제로 기록됐는지는 이 메서드 호출 시점엔
        알 수 없으므로 `marker_written`은 `None`으로 남습니다(디스크
        확정 여부를 사실과 다르게 주장하지 않기 위함).
        """

        self._stop_event.set()
        thread_finished = True
        if self._thread is not None:
            self._thread.join(timeout=self._shutdown_drain_timeout_sec)
            thread_finished = not self._thread.is_alive()

        return {
            "clean_shutdown": thread_finished and self.marker_written is True,
            "queue_drained": thread_finished,
            "marker_written": self.marker_written if thread_finished else None,
            "dropped_count": self.dropped_count,
            "fsync_unconfirmed_count": self.fsync_unconfirmed_count,
            "restart_id": self.restart_id,
            "last_seq_no": self._seq_no,
            "shutdown_at": datetime.now().isoformat(),
        }


# ── 실행 단위 메타데이터 (계좌/환경 ↔ trades.csv 연결) ────────────
#
# 2026-09-18 (지적 3번 반영): `trades.csv`에는 `account_scope_id`/`env`
# 컬럼이 없고, 이번 단계에서 그 스키마를 바꾸지 않습니다(RiskManager의
# 필수 헤더 검증을 건드리지 않기 위함). 이 연결은 새로 만들지 않고
# 기존 `infra/storage/run_baseline.py`(B01, 2026-09-11)를 그대로
# 재사용합니다 — 그 모듈이 이미 프로세스 시작마다 run_id/is_mock/
# is_paper_trading/started_at을 완전히 분리된 파일(run_baseline.csv)에
# 기록하고, trades.csv/signal_log.csv의 timestamp를 그 시간범위로
# 조인하는 `resolve_run_id_for_timestamp()`를 갖고 있었기 때문입니다.
# 이번 라운드에서 그 파일에 `account_scope_id` 필드와
# `resolve_scope_for_trade_timestamp()`(run_id 조인 결과에서
# (account_scope_id, env)를 뽑아주는 얇은 wrapper)만 추가했습니다 —
# 새 조인 로직을 병행해서 만들지 않았습니다. `compute_coverage()`의
# `accepted_orders` 인자는 그 함수의 결과로 미리 만들어 전달하고,
# 연결되는 run이 없는 과거 행은 그 단계에서 "미확인"으로 제외합니다.


# ── 커버리지 계측 ─────────────────────────────────────────────────

COVERAGE_DISABLED = "계측_비활성"


def compute_coverage(
    *,
    account_scope_id: str,
    accepted_orders: set[tuple[str, str, str, str]],   # (account_scope_id, env, order_date, order_id) — order_id는 normalize_order_id() 정규화된 값이어야 함
    observations: list[OrderStatusObservation],
    order_date_resolver,  # Callable[[OrderStatusObservation], str | None] — None이면 미확인
) -> dict:
    """고유 주문 기준 커버리지 4개 지표(2026-09-18 지적 2번의 표를 그대로 채택).

    - `account_scope_id`가 비어있으면 계측 자체를 비활성으로 표시합니다
      (지적 5번 반영 — 0%가 아니라 "계측 비활성").
    - 서로 다른 (env, order_date)의 같은 order_id는 절대 합치지 않습니다
      (지적 3번 반영) — 키 자체가 4-튜플입니다.
    - order_date를 확정할 수 없는 관측은 "미확인" 버킷으로 분리하고
      분모/분자 어디에도 넣지 않습니다.
    - `accepted_orders`에 없는 order_id의 관측은 "고아 관측"으로 분리합니다.

    2026-09-18 재검토 반영(지적 3번, 재현된 버그): 이전 구현은 분모만
    `account_scope_id`로 좁히고 분자(`observed_keys` 등)는 함수에 전달된
    **모든** 관측(다른 계좌 포함)을 다 훑어서, 계좌 A가 1건 접수·1건
    관측인데 계좌 B도 1건 접수·1건 관측이면 A의 관측률이 200%로
    나오는 결함이 재현됐습니다(B의 키도 전체 `accepted_orders`
    집합에는 있으므로 A를 계산할 때도 분자에 더해짐). 이제 루프에
    들어가기 전에 `observations`를 이 함수가 계산 중인
    `account_scope_id`로 먼저 좁혀, 분모·분자·조회 카운트 전부가
    같은 계좌 범위 안에서만 계산됩니다. order_id 비교도 `_find_
    matching()`/`find_all_matching()`이 쓰는 것과 동일한
    `normalize_order_id()`로 정규화해, 0-padding 차이("000123" vs
    "123")로 같은 주문이 고아 관측으로 잘못 분류되지 않게 합니다.

    2026-09-18 재재검토 반영(지적 2번, 재현된 버그): `outcome="partial"`
    (oso/cntr 중 하나만 성공)이 `api_error`가 아니라는 이유만으로
    `query_success`에 들어가, "원문 일부를 확보했다"는 사실이 "조회가
    완전히 성공했다"로 오집계됐습니다(재현: cntr 조회만 실패해도
    query_success_count=1, query_failed_count=0, 주문 관측률 100%로
    나옴). 이제 `outcome`을 success/partial/api_error 세 갈래로 명시
    구분합니다 — partial은 `query_partial_count`로 별도 집계하고,
    `query_success_count`/`observed_keys`(따라서 주문 관측률의 분자)
    어디에도 넣지 않습니다. 부분 증거로는 새 판정(FILLED 등)을 만들지
    않는다는 `_safe_record_order_status_observation()`의 계약과도
    일치합니다(partial 관측은 `psm_broker_status=None`으로 기록됨).
    """

    if not account_scope_id.strip():
        return {"status": COVERAGE_DISABLED, "reason": "account_scope_id 미설정"}

    scoped_observations = [obs for obs in observations if obs.account_scope_id == account_scope_id]

    unresolved_date_count = 0
    orphan_count = 0
    observed_keys: set[tuple[str, str, str, str]] = set()
    filled_keys: set[tuple[str, str, str, str]] = set()
    priced_keys: set[tuple[str, str, str, str]] = set()

    query_attempt = 0
    query_success = 0
    query_partial = 0
    query_failed = 0
    write_success = 0  # 실제 디스크 반영 여부는 이 함수 호출 시점엔 알 수 없음 — 별도 카운터로 다룸(라이터의 dropped_count 참고)

    for obs in scoped_observations:
        query_attempt += 1
        if obs.outcome == "api_error":
            query_failed += 1
            continue
        if obs.outcome == "partial":
            # 부분 조회 성공은 "조회 성공"이 아닙니다 — 완전한 조회
            # 성공과 구분해서 별도로만 집계하고, 아래 관측률 분자에는
            # 포함하지 않습니다(수정 방향: "부분 증거를 관측률에
            # 포함할지는 별도로 정의하되, 조회 성공으로 표시해서는
            # 안 됩니다"를 그대로 반영 — 이번 라운드는 후자만 고정).
            query_partial += 1
            continue
        query_success += 1

        order_date = order_date_resolver(obs)
        if order_date is None:
            unresolved_date_count += 1
            continue

        key = (obs.account_scope_id, obs.env, order_date, normalize_order_id(obs.requested_order_id))
        if key not in accepted_orders:
            orphan_count += 1
            continue

        observed_keys.add(key)
        if obs.psm_broker_status == "FILLED":
            filled_keys.add(key)
            if obs.filled_price_parsed is not None and obs.filled_price_parsed > 0:
                priced_keys.add(key)

    scoped_accepted = {k for k in accepted_orders if k[0] == account_scope_id}
    total_accepted = len(scoped_accepted)
    order_observation_rate = (len(observed_keys) / total_accepted) if total_accepted else None
    status_confirmation_rate = (len(filled_keys) / len(observed_keys)) if observed_keys else None
    price_capture_rate = (len(priced_keys) / len(filled_keys)) if filled_keys else None

    return {
        "status": "계측됨",
        "total_accepted_orders": total_accepted,
        "observed_unique_orders": len(observed_keys),
        "filled_unique_orders": len(filled_keys),
        "priced_unique_orders": len(priced_keys),
        "order_observation_rate": order_observation_rate,
        "status_confirmation_rate": status_confirmation_rate,
        "price_capture_rate": price_capture_rate,
        "query_attempt_count": query_attempt,
        "query_success_count": query_success,
        "query_partial_count": query_partial,
        "query_failed_count": query_failed,
        "unresolved_order_date_count": unresolved_date_count,
        "orphan_observation_count": orphan_count,
    }


def dedupe_by_write_id(
    observations: list[OrderStatusObservation],
) -> tuple[list[OrderStatusObservation], list[str]]:
    """같은 `write_id`가 여러 번 나타나는 경우를 처리합니다.

    2026-09-18 (지적 4번 반영): 내용까지 동일하면 정상적인 재시도로
    간주해 한 번만 집계에 남기고, 내용이 다르면 그 자체가 버그
    신호이므로 조용히 하나를 고르지 않고 `write_id` 목록을 별도로
    반환해 호출부가 오류로 처리하게 합니다.
    """

    by_id: dict[str, list[OrderStatusObservation]] = {}
    for obs in observations:
        by_id.setdefault(obs.write_id, []).append(obs)

    deduped: list[OrderStatusObservation] = []
    conflicting_ids: list[str] = []
    for write_id, group in by_id.items():
        hashes = {g.content_hash() for g in group}
        if len(hashes) == 1:
            deduped.append(group[0])
        else:
            conflicting_ids.append(write_id)
    return deduped, conflicting_ids
