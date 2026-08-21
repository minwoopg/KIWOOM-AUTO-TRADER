from __future__ import annotations

"""1P0.8-E.1-A: Durable Tracked Order Journal (2026-08-20).

**목적 — 이 모듈이 하는 일은 딱 하나뿐입니다**: 실제 브로커
`order_id`가 확보된 BUY/SELL accepted 주문의 사실(fact)을 디스크에
원자적으로 보존합니다. `domain/position/lifecycle.py`의
`PositionStateMachine`은 메모리 전용이라 프로세스가 재시작되면
"이 종목에 BUY_PENDING/SELL_PENDING/orphan 주문이 있었다"는 사실
자체가 통째로 사라집니다 — 이 모듈은 그 사실만 별도로, lifecycle과
독립적으로 살려둡니다.

**이번 라운드(E.1-A)의 범위 — 매우 중요, 반드시 지킬 것**:
```
쓰기 ✅ (accepted + order_id 확정 직후 atomic 저장)
갱신 ✅ (pending → orphan 전환 시 orphaned_at, 첫 체결 시 first_fill_at)
안전한 삭제 ✅ (terminal 상태가 "안전하게" 확정된 뒤에만 — 아래 참고)

startup 자동 lifecycle 복구 ❌
get_order_status() 자동 호출 추가 ❌
BUY/SELL 자동 실행·재주문·cancel_order ❌
새 PSM state(RECOVERING 등) 추가 ❌
D.2(ambiguous placement) 처리 ❌
```
이 파일을 읽어 자동으로 상태를 복구하는 로직은 1P0.8-E.1-B(별도
승인 후 진행)의 책임입니다. E.1-A는 순수하게 "재시작 전 우리가
확실히 알고 있던 사실을 잃지 않는다"는 durable evidence layer만
구현합니다.

**정상 종료와 crash를 구분하지 않습니다**: 레코드 삭제 기준은
프로세스가 정상 종료됐는지가 아니라, 그 주문의 terminal 상태가
안전하게 확인됐는지입니다(`TradingService._maintain_tracked_order_
journal()` 참고) — "정상 종료 버튼을 눌렀다"는 사실 자체는 브로커에
남아있는 주문의 운명과 무관하기 때문입니다.

**원자적 쓰기**: `infra/storage/state_store.py`(`JsonStateStore`)의
평범한 `write_text()`가 아니라, `infra/storage/logger.py`의 CSV
헤더 마이그레이션과 `export_daily_bundle.py`의 ZIP 생성이 쓰는 것과
동일한 관용구를 씁니다 — 같은 디렉터리에 `.tmp` 파일로 먼저 쓰고
`flush()` + `os.fsync()`(실패해도 무결성엔 영향 없어 계속 진행 —
Windows 등 일부 환경에서 읽기전용 유사 핸들에 fsync가 거부되는
사례가 이미 `export_daily_bundle.py` 1I.5에서 실측 확인됨) 후
`os.replace()`로 원자적 교체. 쓰기 도중 실패하면 `.tmp`만 지우고
원본 파일은 절대 건드리지 않습니다.

**손상된 파일을 만나면 조용히 무시하지 않습니다(fail-close)**:
JSON 파싱 실패, 최상위 형식 불일치, `schema_version` 불일치, 개별
레코드 파싱 실패는 모두 `TrackedOrderJournalCorruptError`를
발생시킵니다. 이 예외를 삼키고 "빈 상태로 정상 운영"하는 판단은
호출부(`TradingService`)의 책임이며, 반드시 CRITICAL 로그를 남긴
뒤에만 계속 진행해야 합니다 — 이 모듈 자체는 절대 조용히 넘어가지
않습니다.

**민감정보 없음**: 레코드 필드(symbol/side/order_id/수량/시각)에는
계좌번호·토큰·appkey 등 `export_daily_bundle.py`의 `SENSITIVE_KEYS`
대상 필드가 전혀 없습니다 — order_id는 브로커 주문번호일 뿐 계좌
식별자가 아닙니다.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

# BUY/SELL만 허용 — 그 외 값이 들어오면 저장 자체를 거부합니다
# (호출부 버그를 조용히 저장하지 않기 위한 방어적 검증).
_VALID_SIDES = ("BUY", "SELL")
_VALID_LIFECYCLE_KINDS = ("BUY_PENDING", "SELL_PENDING")


class TrackedOrderJournalCorruptError(Exception):
    """journal 파일이 손상됐거나(JSON 파싱 실패, 형식 불일치) 이
    코드가 아는 schema_version과 다를 때 발생합니다. 절대 이 예외를
    조용히 삼키고 정상 운영하지 마세요 — 반드시 CRITICAL로 노출한
    뒤, 안전한 기본값(빈 journal)으로만 계속 진행해야 합니다.
    """


@dataclass
class TrackedOrderRecord:
    """추적 가능한 accepted 주문 하나의 durable 사실 기록.

    필드는 민우님이 확정한 최소 스키마 그대로입니다 — 더 늘리지
    않습니다(E.1-A는 evidence layer일 뿐, PSM의 축소 복제본이
    아닙니다).
    """

    symbol: str
    side: str                          # "BUY" | "SELL"
    order_id: str
    base_quantity_before_order: int
    target_quantity_after_order: int
    accepted_at: datetime
    lifecycle_kind: str                # "BUY_PENDING" | "SELL_PENDING"
    first_fill_at: datetime | None = None
    orphaned_at: datetime | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.side not in _VALID_SIDES:
            raise ValueError(f"side는 BUY/SELL만 허용됩니다: {self.side!r}")
        if self.lifecycle_kind not in _VALID_LIFECYCLE_KINDS:
            raise ValueError(
                f"lifecycle_kind는 BUY_PENDING/SELL_PENDING만 허용됩니다: "
                f"{self.lifecycle_kind!r}"
            )
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol이 비어 있습니다")
        if not self.order_id or not self.order_id.strip():
            # sentinel/placeholder("pending"/"UNKNOWN_ORDER_ID"/빈 값) 저장
            # 금지는 호출부(TradingService, is_trackable_order_id() 사용)의
            # 1차 책임이지만, 이 레코드 자체도 마지막 방어선으로 빈
            # order_id를 거부합니다.
            raise ValueError("order_id가 비어 있습니다 — 추적 불가능한 값은 저장하지 않습니다")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "side": self.side,
            "order_id": self.order_id,
            "base_quantity_before_order": self.base_quantity_before_order,
            "target_quantity_after_order": self.target_quantity_after_order,
            "accepted_at": self.accepted_at.isoformat(),
            "lifecycle_kind": self.lifecycle_kind,
            "first_fill_at": (
                self.first_fill_at.isoformat() if self.first_fill_at else None
            ),
            "orphaned_at": (
                self.orphaned_at.isoformat() if self.orphaned_at else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrackedOrderRecord":
        return cls(
            symbol=str(d["symbol"]),
            side=str(d["side"]),
            order_id=str(d["order_id"]),
            base_quantity_before_order=int(d["base_quantity_before_order"]),
            target_quantity_after_order=int(d["target_quantity_after_order"]),
            accepted_at=datetime.fromisoformat(d["accepted_at"]),
            lifecycle_kind=str(d["lifecycle_kind"]),
            first_fill_at=(
                datetime.fromisoformat(d["first_fill_at"])
                if d.get("first_fill_at") else None
            ),
            orphaned_at=(
                datetime.fromisoformat(d["orphaned_at"])
                if d.get("orphaned_at") else None
            ),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


class TrackedOrderJournalStore:
    """`TrackedOrderRecord`를 심볼별로 1건씩, JSON 파일 하나에 원자적으로
    저장/조회하는 저장소. `JsonStateStore`와 이름은 비슷하지만 쓰기
    방식은 다릅니다(위 모듈 docstring의 "원자적 쓰기" 참고) — 이
    저장소를 새로 만드는 이유가 바로 그 차이입니다.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> dict[str, TrackedOrderRecord]:
        """파일이 없으면 빈 dict. 손상됐거나 schema_version이 다르면
        `TrackedOrderJournalCorruptError`를 던집니다(절대 조용히
        빈 dict로 대체하지 않음 — 그건 호출부가 명시적으로 CRITICAL
        로그를 남긴 뒤에만 선택할 수 있는 fallback입니다).
        """
        if not self.path.exists():
            return {}
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TrackedOrderJournalCorruptError(
                f"{self.path}: 파일을 읽을 수 없음 — {type(exc).__name__}: {exc}"
            ) from exc
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise TrackedOrderJournalCorruptError(
                f"{self.path}: JSON 파싱 실패 — {exc}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("records"), dict):
            raise TrackedOrderJournalCorruptError(
                f"{self.path}: 최상위 형식이 예상과 다름"
                f"(dict + 'records' dict 키 필요, 실제 타입={type(raw).__name__})"
            )
        file_schema_version = raw.get("schema_version")
        if file_schema_version != SCHEMA_VERSION:
            raise TrackedOrderJournalCorruptError(
                f"{self.path}: schema_version 불일치 "
                f"(파일={file_schema_version!r}, 코드={SCHEMA_VERSION})"
            )
        records: dict[str, TrackedOrderRecord] = {}
        for symbol, raw_record in raw["records"].items():
            try:
                records[symbol] = TrackedOrderRecord.from_dict(raw_record)
            except (KeyError, ValueError, TypeError) as exc:
                raise TrackedOrderJournalCorruptError(
                    f"{self.path}: '{symbol}' 레코드 파싱 실패 — "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return records

    def get(self, symbol: str) -> TrackedOrderRecord | None:
        return self.load_all().get(symbol)

    def upsert(self, record: TrackedOrderRecord) -> None:
        """이 심볼의 레코드를 생성하거나 덮어씁니다. 기존 파일이
        손상돼 있으면(다른 종목 레코드가 있을 수 있음) 그걸 모르고
        덮어써서 유실시키지 않도록, 먼저 `load_all()`로 전체를
        읽어서 실패하면 그대로 전파합니다(호출부가 CRITICAL로
        처리).
        """
        records = self.load_all()
        records[record.symbol] = record
        self._atomic_write(records)

    def remove(self, symbol: str) -> None:
        """이 심볼의 active 레코드를 삭제합니다(terminal 상태가
        안전하게 확정된 뒤에만 호출부가 불러야 함 — 이 메서드 자체는
        "언제 지울지"를 판단하지 않습니다). 레코드가 없으면 아무
        것도 하지 않습니다(idempotent).
        """
        records = self.load_all()
        if symbol not in records:
            return
        del records[symbol]
        self._atomic_write(records)

    def _atomic_write(self, records: dict[str, TrackedOrderRecord]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "records": {sym: rec.to_dict() for sym, rec in records.items()},
        }
        # 같은 디렉터리에 pid로 구분된 고유 임시 파일 — os.replace()가
        # 같은 파일시스템 안에서 원자적으로 동작하려면 목적지와 같은
        # 디렉터리에 있어야 함(export_daily_bundle.py와 동일 원칙).
        tmp_path = self.path.with_suffix(
            self.path.suffix + f".{os.getpid()}.tmp"
        )
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # 2026-08-06 (export_daily_bundle.py 1I.5)와 동일한
                    # 판단: 일부 환경(Windows 등)에서 fsync가 거부될 수
                    # 있으나, os.replace() 자체의 원자성은 이와 무관하게
                    # 보장되므로 fsync 실패는 치명적이지 않음.
                    pass
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise
