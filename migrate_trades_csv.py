# -*- coding: utf-8 -*-
"""
logs/trades.csv 손상 행 정리 스크립트 (2026-07-22, 7.27~7.29절)

배경: RiskManager의 손익 계산 엄격검증(fail-close)을 실제로 적용하려면
trades.csv에 필드가 밀린 손상 행이 없어야 합니다. 이 스크립트는
trades.csv를 검사해서 구조적으로 손상된 행을 별도 archive 파일로
옮기고, 나머지 행을 trades.csv에 남깁니다.

⚠️ 이 스크립트는 봇을 완전히 종료한 상태에서만 사용하세요. 파일
스냅샷(mtime/size) 비교로 실행 중 변경을 감지하지만, 이건 "우연히
그 순간에 걸리면 잡아내는" 수준의 보조 안전장치일 뿐 완전한 잠금은
아닙니다(스냅샷 비교와 실제 교체 사이에도 아주 짧은 경쟁 구간이
남아있음, 2026-07-22 7차 GPT 코드리뷰 지적). 진짜 안전은 "봇이
꺼져 있는 상태"에서만 보장됩니다.

⚠️ 이 스크립트가 "정상(structurally_valid)"으로 분류하는 기준은
RiskManager의 엄격 검증(symbol 6자리/price 양수/side BUY·SELL 등)
보다 느슨합니다 — 여기서는 "CSV 구조가 깨지지 않았는지"만 봅니다.
구조는 멀쩡하지만 내용이 이상한 행(예: symbol이 6자리가 아니거나
price가 0)은 이 스크립트를 통과해도 RiskManager에서 다시 걸릴 수
있습니다 — 이건 정상입니다(실제로 손상된 날짜를 정확히 차단하는
목적).

안전장치:
  1) --dry-run(기본값)으로 먼저 결과만 미리 볼 수 있습니다
  2) 실제 적용 전후로 파일 크기/수정시각을 비교해 그 사이 파일이
     바뀌었으면(봇이 실행 중일 가능성) 중단합니다 — 읽은 직후와
     원자적 교체 직전 두 번 확인
  3) 원자적 교체(임시 파일 작성 후 os.replace) — 쓰기 도중 중단돼도
     원본이 손상되지 않습니다
  4) 백업/archive 파일명에 마이크로초+짧은 UUID를 넣어 같은 초에
     여러 번 실행해도 절대 덮어쓰지 않습니다
  5) archive에는 원본 줄 번호와 손상 사유를 함께 기록하고, 별도
     manifest(JSON)를 교체 완료 후 "completed" 상태로 남깁니다

사용법 (PowerShell):
    python migrate_trades_csv.py                    # 미리보기만 (dry-run 기본)
    python migrate_trades_csv.py --apply             # 실제 적용 (확인 프롬프트 있음)
    python migrate_trades_csv.py --apply --yes       # 실제 적용 (확인 생략)
    python migrate_trades_csv.py --file logs\\trades.csv --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path


def classify_row(row: list[str], header: list[str], accepted_idx: int) -> str | None:
    """행이 구조적으로 손상됐으면 그 사유 문자열을, 아니면 None을 반환합니다.

    ⚠️ 여기서 "정상"은 "CSV 구조가 깨지지 않았다"는 뜻이지, "모든
    필드값이 유효하다"는 뜻이 아닙니다(2026-07-22 7차 GPT 코드리뷰
    지적 — RiskManager의 엄격 검증(symbol 형식, price 양수 등)과
    기준이 다릅니다). 이 함수를 통과한 행도 RiskManager에서 다시
    fail-close될 수 있고, 그건 정상 동작입니다.
    """
    if len(row) != len(header):
        return f"column_count_mismatch(expected={len(header)}, actual={len(row)})"
    if accepted_idx < len(row):
        val = row[accepted_idx].strip().lower()
        if val not in ("true", "false"):
            return f"invalid_accepted({val!r})"
    return None


def snapshot(path: Path) -> tuple[int, int]:
    """파일의 (mtime_ns, size)를 반환 — 변경 감지용."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def make_unique_stamp() -> str:
    """마이크로초 + 짧은 UUID로 거의 확실히 고유한 문자열을 만듭니다.

    2026-07-22 (7차 수정, GPT 코드리뷰): 초 단위 타임스탬프만 쓰면
    같은 초에 두 번 실행할 때 파일명이 겹칠 수 있었음(백업은
    shutil.copy2가 존재 확인 없이 조용히 덮어씀). 마이크로초와
    UUID 일부를 추가해 실질적으로 충돌 불가능하게 함.
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", default="logs/trades.csv",
        help="정리할 trades.csv 경로 (기본: logs/trades.csv)",
    )
    parser.add_argument(
        "--archive-dir", default="logs/archive",
        help="손상 행을 옮길 archive 디렉토리 (기본: logs/archive)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="실제로 파일을 수정합니다. 지정하지 않으면 결과만 미리 보여줍니다(dry-run).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="--apply와 함께 사용 시 확인 프롬프트를 건너뜁니다.",
    )
    args = parser.parse_args()

    trade_log = Path(args.file)
    if not trade_log.exists():
        print(f"[오류] 파일을 찾을 수 없습니다: {trade_log}")
        return 1

    snapshot_at_read = snapshot(trade_log)

    with trade_log.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            print("[오류] 파일이 비어 있습니다 (헤더 없음)")
            return 1

        if "accepted" not in header:
            print(f"[오류] 'accepted' 컬럼을 찾을 수 없습니다. 헤더: {header}")
            return 1
        accepted_idx = header.index("accepted")

        # 2026-07-22 (7차 수정, GPT 코드리뷰): "정상 행"이라는 이름이
        # RiskManager의 엄격 검증과 기준이 다르다는 오해를 줄 수 있어
        # structurally_valid/structurally_corrupted로 명확히 함.
        structurally_valid_rows: list[list[str]] = []
        structurally_corrupted_rows: list[tuple[int, str, list[str]]] = []  # (원본줄번호, 사유, row)
        for line_no, row in enumerate(reader, start=2):
            reason = classify_row(row, header, accepted_idx)
            if reason is not None:
                structurally_corrupted_rows.append((line_no, reason, row))
            else:
                structurally_valid_rows.append(row)

    total = len(structurally_valid_rows) + len(structurally_corrupted_rows)
    print(
        f"전체 {total}건 중 구조적으로 정상 {len(structurally_valid_rows)}건, "
        f"손상 {len(structurally_corrupted_rows)}건"
    )
    print(
        "(참고: 여기서 '정상'은 CSV 구조만 확인한 것입니다. 개별 필드값의 "
        "유효성은 RiskManager가 운영 중 별도로 엄격하게 검증합니다.)"
    )

    if not structurally_corrupted_rows:
        print("구조적으로 손상된 행이 없습니다. 정리할 필요 없음.")
        return 0

    print("\n손상 행 샘플 (최대 5건):")
    for line_no, reason, row in structurally_corrupted_rows[:5]:
        print(f"  {line_no}행 [{reason}]: {row[:6]}")

    if not args.apply:
        print("\n[dry-run] 실제로 파일을 수정하지 않았습니다. --apply를 추가하면 실제 적용됩니다.")
        return 0

    print("\n주의: 자동매매 봇이 완전히 종료된 상태인지 반드시 확인하세요.")
    if not args.yes:
        answer = input(
            f"{len(structurally_corrupted_rows)}건을 archive로 옮기고 {trade_log}를 "
            f"나머지 {len(structurally_valid_rows)}건만 남기도록 덮어쓰시겠습니까? [y/N] "
        )
        if answer.strip().lower() != "y":
            print("취소되었습니다.")
            return 0

    # 2026-07-22 (6차 수정): 파일을 읽은 시점과 지금 사이에 봇이 새
    # 거래를 append했을 수 있음 — 그 사이 파일이 바뀌었으면 중단.
    if snapshot(trade_log) != snapshot_at_read:
        print(
            "\n[오류] 작업 중 trades.csv가 변경됐습니다(봇이 실행 중일 "
            "가능성). 적용을 중단합니다. 봇을 완전히 종료한 뒤 다시 "
            "시도하세요."
        )
        return 2

    stamp = make_unique_stamp()

    # 백업 (마이크로초+UUID로 고유 파일명 — 같은 초에 여러 번 실행해도 안전)
    backup_path = trade_log.with_name(f"{trade_log.name}.{stamp}.bak")
    if backup_path.exists():
        print(f"[오류] 백업 파일이 이미 존재합니다: {backup_path} (재시도하세요)")
        return 2
    shutil.copy2(trade_log, backup_path)
    print(f"백업 생성: {backup_path}")

    # archive 저장
    archive_dir = Path(args.archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"trades_corrupted_{stamp}.csv"
    if archive_path.exists():
        print(f"[오류] archive 파일이 이미 존재합니다: {archive_path} (재시도하세요)")
        return 2

    with archive_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_line", "corruption_reason"] + header)
        for line_no, reason, row in structurally_corrupted_rows:
            writer.writerow([line_no, reason] + row)
    print(f"손상 행 {len(structurally_corrupted_rows)}건 -> {archive_path} (원본 줄번호/사유 포함)")

    # 2026-07-22 (7차 수정, GPT 코드리뷰): manifest를 교체 *이전*에
    # "완료"로 기록하면, 그 뒤 os.replace()가 실패했을 때 manifest만
    # 완료 상태로 남고 실제 파일은 안 바뀐 모순이 생김. 준비 단계는
    # "prepared"로 먼저 남기고, 실제 교체가 성공한 뒤에만 "completed"
    # 로 갱신.
    manifest_path = archive_dir / f"trades_corrupted_{stamp}.manifest.json"
    manifest = {
        "source_file": str(trade_log),
        "prepared_at": datetime.now().isoformat(),
        "total_rows": total,
        "structurally_valid_rows": len(structurally_valid_rows),
        "structurally_corrupted_rows": len(structurally_corrupted_rows),
        "backup_path": str(backup_path),
        "archive_path": str(archive_path),
        "status": "prepared",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2026-07-22 (7차 수정, GPT 코드리뷰): 원자적 교체 — 같은
    # 디렉터리에 임시 파일을 완전히 작성한 뒤 os.replace()로 교체.
    # 교체 직전에 스냅샷을 한 번 더 확인해서, 백업/archive/manifest
    # 작성에 걸린 시간 동안 봇이 새 거래를 추가했을 가능성까지 방어
    # (완전한 잠금은 아니지만 경쟁 구간을 최대한 좁힘 — docstring의
    # 한계 설명 참고).
    fd, tmp_name = tempfile.mkstemp(
        dir=str(trade_log.parent), suffix=".tmp", prefix=trade_log.stem + "_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(header)
            writer.writerows(structurally_valid_rows)
            fp.flush()
            os.fsync(fp.fileno())

        if snapshot(trade_log) != snapshot_at_read:
            print(
                "\n[오류] 정리 파일 작성 중 trades.csv가 다시 변경됐습니다. "
                "원본 교체를 중단합니다. 새 거래가 유실되지 않도록 안전하게 "
                "멈춥니다 — 봇을 완전히 종료한 뒤 다시 시도하세요."
            )
            os.unlink(tmp_name)
            manifest["status"] = "aborted_file_changed"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return 2

        os.replace(tmp_name, trade_log)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        manifest["status"] = "failed"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise

    manifest["status"] = "completed"
    manifest["completed_at"] = datetime.now().isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"정리 이력 기록 -> {manifest_path}")
    print(f"나머지 {len(structurally_valid_rows)}건 -> {trade_log} (원자적 교체 완료)")

    print("\n완료. 문제가 있으면 백업 파일로 복원하세요:")
    print(f"  Copy-Item {backup_path} {trade_log} -Force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
