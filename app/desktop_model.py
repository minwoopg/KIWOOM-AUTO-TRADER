"""Local read-only dashboard data and owned-process control; no broker imports."""
from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime

from app.desktop_control import STOP_FILE_ENV
from config.settings import load_settings
from domain.cost_model import load_cost_model
from analyze_trades import pair_trades
from export_daily_bundle import mask
from utils.time_utils import KST_TZ

ROOT = Path(__file__).resolve().parents[1]


def today_string():
    return datetime.now(KST_TZ).date().isoformat()


def read_tail(path: Path, limit=64 * 1024) -> str:
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size-limit))
            if size > limit:
                f.readline()
            return mask(f.read(limit).decode('utf-8-sig', errors='replace'))
    except OSError:
        return '(로그 없음 또는 읽기 실패)'


def csv_rows(path: Path, limit: int, *, allow_tail=False):
    # Trade totals require complete input; signal frequencies may use a labelled tail.
    notice = ''
    try:
        with path.open('rb') as f:
            raw = f.read(limit+1)
            if len(raw) > limit:
                if not allow_tail:
                    return [], '크기 제한 초과 — 집계 생략'
                f.seek(0)
                header = f.readline(65536)
                f.seek(0, 2)
                f.seek(max(len(header), f.tell()-limit))
                f.readline()  # discard the first potentially incomplete record
                raw = header + f.read(limit)
                notice = '최근 신호 일부만 집계 — 전체 일일 건수 아님'
        text = raw.decode('utf-8-sig')
        if not text or 'timestamp' not in next(csv.reader(io.StringIO(text)), []):
            return [], 'CSV 헤더 없음 또는 형식 불일치'
        if text and not text.endswith(('\n', '\r')):
            return [], '기록 중인 행 — 다음 갱신 대기'
        rows = list(csv.DictReader(io.StringIO(text), strict=True))
        if any(None in row or None in row.values() for row in rows):
            return [], 'CSV 열 불일치 — 집계 보류'
        return rows, notice
    except (OSError, UnicodeError, csv.Error):
        return [], '파일 없음 또는 읽기 실패'


def on_day(row, day):
    try:
        stamp = datetime.fromisoformat(row.get('timestamp', ''))
        stamp = stamp.replace(tzinfo=KST_TZ) if stamp.tzinfo is None else stamp.astimezone(KST_TZ)
        return stamp.date().isoformat() == day
    except (ValueError, TypeError):
        return False


def file_stamp(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, KST_TZ).strftime('%m-%d %H:%M:%S')
    except OSError:
        return '없음'


def snapshot(root=ROOT, day=None):
    day = day or today_string()
    settings = load_settings(root / 'config/settings.yaml')
    storage = settings.storage
    trades_path = root / storage.trade_log_file
    signal_path = root / storage.signal_log_file
    state_path = root / storage.state_file
    trades, trades_error = csv_rows(trades_path, 16*1024*1024)
    signals, signals_error = csv_rows(signal_path, 4*1024*1024, allow_tail=True)
    trades = [r for r in trades if on_day(r, day)]
    signals = [r for r in signals if on_day(r, day)]
    pairs = pair_trades(trades)
    model = load_cost_model(root / 'config/settings.yaml', use_cache=False)
    gross = sum(p['pnl_amount'] for p in pairs)
    notional = sum(p['buy_price'] * p['quantity'] for p in pairs)
    blocks = Counter(r.get('order_block_reason') or '(사유 없음)' for r in signals if r.get('final_decision') == 'BLOCKED')
    state_note = '실제 보유·미체결은 이 화면에서 조회하지 않습니다.'
    try:
        with state_path.open('rb') as f:
            data = f.read(8*1024*1024+1)
        if len(data) > 8*1024*1024:
            raise ValueError('state too large')
        state = json.loads(data.decode('utf-8-sig'))
        intents = state.get('unresolved_order_intents', {})
        if not isinstance(intents, dict):
            raise ValueError('invalid intents')
        state_note += f" 로컬 미해결 주문 의도: {len(intents)}건 (계좌 미체결 건수 아님)."
    except (OSError, ValueError, TypeError, AttributeError):
        state_note += ' 로컬 상태 파일 확인 불가.'
    return dict(day=day, mode='가상 Mock' if settings.broker.use_mock else ('모의투자' if settings.broker.is_paper_trading else '실전투자'),
                trades=trades[-200:], trade_count=len(trades), pairs=len(pairs), gross=None if trades_error else gross,
                base=None if trades_error else gross-model.cost_amount(notional, 'base'), base_pct=model.base_roundtrip_pct,
                blocks=blocks.most_common(30), trades_error=trades_error, signals_error=signals_error,
                updated=datetime.now(KST_TZ).strftime('%H:%M:%S'),
                timestamps=f'거래 {file_stamp(trades_path)} / 신호 {file_stamp(signal_path)} / 상태 {file_stamp(state_path)}',
                state_note=state_note, log=read_tail(root / storage.app_log_file),
                vwap=settings.trading.require_entry_above_vwap, max_symbols=settings.websocket.max_symbols)


class ProcessController:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.process = None
        self.stop_file = None
        self.stopping = False
        self.console_path = self.root / 'logs/desktop_child.log'
        self.export_process = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running:
            raise RuntimeError('이미 이 화면에서 실행 중입니다.')
        self.root.joinpath('data/desktop').mkdir(parents=True, exist_ok=True)
        self.stop_file = self.root / 'data/desktop' / (uuid.uuid4().hex + '.stop')
        self.stop_file.with_suffix('.owner').write_text('GUI-owned process; stop token only\n', encoding='ascii')
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        env[STOP_FILE_ENV] = str(self.stop_file)
        self.console_path.parent.mkdir(parents=True, exist_ok=True)
        # A real file avoids unread PIPE deadlocks. Each start resets only this GUI console log.
        try:
            with self.console_path.open('wb') as console:
                self.process = subprocess.Popen([sys.executable, '-u', '-m', 'app.main'], cwd=self.root, env=env,
                    stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except OSError:
            self.stop_file.with_suffix('.owner').unlink(missing_ok=True)
            raise
        self.stopping = False

    def start_failed_fast(self):
        """True once the just-started process has already exited with a non-zero
        code (e.g. blocked by the single-instance lock because another run — a
        terminal session, typically — already holds it). A graceful stop always
        exits 0, so this never fires for a normal shutdown; it only flags a
        launch that never really started. Best-effort only: a slow failure past
        the GUI's short grace window still surfaces through the regular status
        text on the next tick, same as before this check existed.
        """
        return self.process is not None and self.process.poll() is not None and self.process.returncode != 0

    def request_previous_stops(self):
        count = 0
        for path in (self.root / 'data/desktop').glob('*.owner'):
            if re.fullmatch(r'[0-9a-f]{32}\.owner', path.name):
                path.with_suffix('.stop').write_text('stop\n', encoding='ascii')
                count += 1
        return count

    def request_stop(self):
        if not self.running:
            return
        self.stop_file.write_text('stop\n', encoding='ascii')
        self.stopping = True

    def status(self):
        if self.running:
            return '종료 요청 중 · 현재 순회/통신 완료 대기' if self.stopping else f'프로세스 실행 중 · PID {self.process.pid}'
        if self.process is None:
            return '이 화면에서 실행하지 않음 · 외부 실행 여부 미확인'
        if self.stop_file:
            self.stop_file.unlink(missing_ok=True)
            self.stop_file.with_suffix(".owner").unlink(missing_ok=True)
        return f'프로세스 종료 · 종료 코드 {self.process.returncode}'

    def export(self, day):
        datetime.strptime(day, '%Y-%m-%d')
        if self.export_process and self.export_process.poll() is None:
            raise RuntimeError('이미 자료를 내보내는 중입니다.')
        path = self.root / 'logs/desktop_export.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as out:
            self.export_process = subprocess.Popen([sys.executable, '-u', 'export_daily_bundle.py', day], cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
