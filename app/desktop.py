"""Local Windows dashboard. Opening this window never starts trading."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from app.desktop_model import ROOT, ProcessController, snapshot, today_string, read_tail, log
from config.settings import load_settings
from infra.storage.process_lock import single_instance_lock


class Dashboard:
    def __init__(self, window, root=ROOT):
        self.window, self.root = window, Path(root)
        self.control = ProcessController(root)
        self.results = queue.Queue()
        self.loading = False
        self.closing = False
        self.export_seen = None
        self.day = tk.StringVar(value=today_string())
        self.status = tk.StringVar(value='상태 확인 중')
        self.info = tk.StringVar(value='로컬 파일을 읽는 중입니다. 자동매매는 시작하지 않았습니다.')
        self.details = tk.StringVar(value='')
        self.cards = {k: tk.StringVar(value='—') for k in ('mode', 'orders', 'gross', 'base')}
        window.title('KIWOOM · 단타 관리')
        window.geometry('1160x820')
        window.minsize(920, 690)
        window.configure(bg='#eef2f6')
        style = ttk.Style(window)
        style.theme_use('clam')
        font = '맑은 고딕' if os.name == 'nt' else 'DejaVu Sans'
        style.configure('.', font=(font, 10))
        style.configure('TFrame', background='#eef2f6')
        style.configure('TLabel', background='#eef2f6', foreground='#1c2c43')
        style.configure('Title.TLabel', font=(font, 21, 'bold'))
        style.configure('Card.TLabel', font=(font, 17, 'bold'), background='white')
        style.configure('Card.TFrame', background='white')
        style.configure('Caption.TLabel', background='white', foreground='#60728a')
        style.configure('TButton', padding=(13, 8))
        style.configure('Treeview', rowheight=28, background='white', fieldbackground='white')
        outer = ttk.Frame(window, padding=20)
        outer.pack(fill='both', expand=True)
        title = ttk.Frame(outer)
        title.pack(fill='x')
        ttk.Label(title, text='KIWOOM  /  단타 관리', style='Title.TLabel').pack(side='left')
        ttk.Label(title, text='조회 날짜 (한국 시간)').pack(side='right', padx=8)
        entry = ttk.Entry(title, textvariable=self.day, width=12)
        entry.pack(side='right')
        entry.bind('<Return>', lambda event: self.refresh(manual=True))
        ttk.Label(outer, textvariable=self.status, font=(font, 11, 'bold')).pack(anchor='w', pady=(12, 8))
        buttons = ttk.Frame(outer)
        buttons.pack(fill='x', pady=(0, 12))
        self.start_button = ttk.Button(buttons, text='▶ 자동매매 시작', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(buttons, text='■ 정상 종료', command=self.stop)
        self.stop_button.pack(side='left', padx=8)
        ttk.Button(buttons, text='새로고침', command=lambda: self.refresh(manual=True)).pack(side='left')
        ttk.Button(buttons, text='이전 창 실행 종료 요청', command=self.recover_stop).pack(side='left', padx=8)
        ttk.Button(buttons, text='보고서 폴더', command=lambda: self.open_folder('reports')).pack(side='right')
        self.export_button = ttk.Button(buttons, text='분석 ZIP 내보내기', command=self.export)
        self.export_button.pack(side='right', padx=8)
        cards = ttk.Frame(outer)
        cards.pack(fill='x')
        for column, (key, caption) in enumerate((('mode', '설정된 실행 모드'), ('orders', '조회일 주문 기록 / 대응 쌍'), ('gross', '비용 전 추정 손익'), ('base', 'Base 비용 후 추정 손익'))):
            cards.columnconfigure(column, weight=1, uniform='cards')
            frame = ttk.Frame(cards, style='Card.TFrame', padding=12)
            frame.grid(row=0, column=column, sticky='nsew', padx=(0, 8) if column<3 else 0)
            ttk.Label(frame, text=caption, style='Caption.TLabel').pack(anchor='w')
            ttk.Label(frame, textvariable=self.cards[key], style='Card.TLabel').pack(anchor='w', pady=(8, 0))
        ttk.Label(outer, text='주문 접수 ≠ 체결 확정  ·  손익은 동일 날짜·동일 수량 주문가격 기준 추정입니다. 계좌 실현손익이 아닙니다.', wraplength=1080).pack(anchor='w', pady=(10, 4))
        ttk.Label(outer, textvariable=self.info, wraplength=1080).pack(anchor='w')
        ttk.Label(outer, textvariable=self.details, wraplength=1080).pack(anchor='w', pady=(4, 10))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill='both', expand=True)
        self.trades = self.table(tabs, '주문 내역 · 최근 200건', [('time','시간',155), ('symbol','종목',80), ('side','매수/매도',80), ('qty','수량',65), ('price','기록 가격',100), ('accepted','접수',60), ('reason','사유',420)])
        self.blocks = self.table(tabs, '진입 차단 사유 · 조회일 최근 기록', [('reason','차단 코드',650), ('count','건수',150)])
        self.logs = self.log_tab(tabs, '매매 로그')
        self.console = self.log_tab(tabs, '실행 / 내보내기 로그')
        ttk.Label(outer, text='종료 요청은 현재 순회가 끝난 후 처리됩니다. 완료 전 주문이 이어질 수 있으며, 종료 후에는 보유종목 자동 감시가 멈춥니다.', wraplength=1080).pack(anchor='w', pady=(10, 0))
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.tick()

    def table(self, tabs, caption, columns):
        frame = ttk.Frame(tabs)
        tabs.add(frame, text=caption)
        tree = ttk.Treeview(frame, columns=[x[0] for x in columns], show='headings')
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=50, stretch=key=='reason')
        v = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        h = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=v.set, xscrollcommand=h.set)
        tree.grid(row=0, column=0, sticky='nsew'); v.grid(row=0, column=1, sticky='ns'); h.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        return tree

    def log_tab(self, tabs, caption):
        frame = ttk.Frame(tabs); tabs.add(frame, text=caption)
        text = ScrolledText(frame, wrap='none', background='#142136', foreground='#dce8f8', font=('Consolas', 10), state='disabled')
        text.pack(fill='both', expand=True)
        return text

    def error(self, exc):
        messagebox.showerror('확인 필요', str(exc), parent=self.window)

    def start(self):
        try:
            cfg = load_settings(self.root / 'config/settings.yaml')
            mode = '가상 Mock' if cfg.broker.use_mock else ('모의투자' if cfg.broker.is_paper_trading else '실전투자')
            if not messagebox.askyesno('자동매매 시작', f'{mode} 설정으로 시작합니다.\n기존 설정에 따라 주문이 제출될 수 있습니다.\n\n시작하시겠습니까?', parent=self.window):
                return
            self.control.start()
            log(f'자동매매 시작 요청 — 모드={mode}, PID={self.control.process.pid}')
            # Best-effort only: catches the common case of a terminal-run
            # instance already holding the single-instance lock, so the user
            # isn't left staring at "실행 중" for 2+ seconds before it dies.
            # A slower failure still surfaces through the regular status text.
            self.window.after(900, self.check_start_result)
        except Exception as exc:
            log(f'자동매매 시작 실패: {exc}')
            self.error(exc)

    def check_start_result(self):
        if self.control.start_failed_fast():
            log(f'자동매매 시작 실패 확인 — 종료 코드 {self.control.process.returncode}(이미 다른 곳에서 실행 중일 가능성)')
            self.error(RuntimeError(
                f'자동매매 시작 실패 (종료 코드 {self.control.process.returncode}).\n'
                '이미 다른 곳(터미널 등)에서 실행 중인지 확인해 주세요.\n\n'
                + read_tail(self.control.console_path)[-600:]
            ))

    def stop(self):
        if not self.control.running:
            return
        if messagebox.askyesno('정상 종료 요청', '종료는 전량 매도가 아닙니다.\n실제 보유·미체결 내역은 키움에서 확인해 주세요.\n현재 순회 완료 후 종료하며, 그 전에는 주문이 이어질 수 있습니다.\n종료 후 보유종목 감시가 중단됩니다.\n\n종료를 요청하시겠습니까?', parent=self.window):
            try:
                self.control.request_stop()
                log(f'정상 종료 요청 전송 — PID={self.control.process.pid}')
            except OSError as exc:
                log(f'종료 요청 실패: {exc}')
                self.error(exc)

    def recover_stop(self):
        if messagebox.askyesno('이전 GUI 실행 복구', '관리 창이 비정상 종료되어 남은 실행에 종료를 요청합니다.\n이 저장소의 GUI 실행 기록에만 요청하며, 실제 종료 여부는 로그로 확인해야 합니다.\n종료 후 보유종목 감시는 멈춥니다. 요청하시겠습니까?', parent=self.window):
            try:
                count = self.control.request_previous_stops()
                log(f'이전 창 실행 종료 요청 — {count}건(이 저장소의 GUI 실행 기록 기준)')
                messagebox.showinfo('종료 요청', f'{count}개 GUI 실행 기록에 요청했습니다. 실제 실행 건수는 아닙니다.\n수동 터미널 실행에는 영향을 주지 않습니다.', parent=self.window)
            except OSError as exc:
                log(f'이전 창 종료 요청 실패: {exc}')
                self.error(exc)

    def close(self):
        if self.control.running:
            self.stop()
            if self.control.stopping:
                self.closing = True
            return
        if self.control.export_process and self.control.export_process.poll() is None:
            messagebox.showinfo('자료 생성 중', 'ZIP 생성이 끝난 후 창을 닫아 주세요.', parent=self.window)
            return
        self.window.destroy()

    def open_folder(self, name):
        try:
            path = self.root / name
            path.mkdir(parents=True, exist_ok=True)
            if os.name == 'nt':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', str(path)])
            log(f'폴더 열기 — {name}')
        except OSError as exc:
            log(f'폴더 열기 실패({name}): {exc}')
            self.error(exc)

    def export(self):
        try:
            self.control.export(self.day.get())
            log(f'분석 ZIP 내보내기 시작 — 조회일 {self.day.get()}')
        except Exception as exc:
            log(f'ZIP 내보내기 실패: {exc}')
            self.error(exc)

    def refresh(self, manual=False):
        if self.loading:
            return
        day = self.day.get()
        if manual:
            # 2초마다 자동으로도 호출되는 함수라, 버튼/Enter로 명시적으로
            # 눌렀을 때만(manual=True) 터미널에 남긴다 — 그렇지 않으면
            # 2초마다 스팸이 됨.
            log(f'새로고침 요청 — 조회일 {day}')
        self.loading = True
        def read():
            try:
                from datetime import datetime
                datetime.strptime(day, '%Y-%m-%d')
                result = snapshot(self.root, day)
                result['console'] = read_tail(self.control.console_path) + '\n\n--- ZIP 내보내기 ---\n' + read_tail(self.root / 'logs/desktop_export.log')
                self.results.put((day, result, None))
            except Exception as exc:
                self.results.put((day, None, str(exc)))
        threading.Thread(target=read, daemon=True).start()

    def render(self, data):
        self.cards['mode'].set(data['mode'])
        self.cards['orders'].set('확인 불가' if data['trades_error'] else f"{data['trade_count']}건 / {data['pairs']}쌍")
        for key in ('gross', 'base'):
            self.cards[key].set('확인 불가' if data[key] is None else f"{data[key]:+,.0f}원")
        self.info.set(f"조회 {data['day']} · 화면 갱신 {data['updated']} · 파일 수정: {data['timestamps']}")
        warnings = ' / '.join(x for x in (data['trades_error'], data['signals_error']) if x)
        self.details.set(f"VWAP 진입 검사 {'켜짐' if data['vwap'] else '꺼짐'} · 감시 한도 {data['max_symbols']} · Base 왕복 비용 {data['base_pct']:.2f}%\n{data['state_note']} {warnings}")
        for tree in (self.trades, self.blocks):
            tree.delete(*tree.get_children())
        for row in reversed(data['trades']):
            self.trades.insert('', 'end', values=(row.get('timestamp',''), row.get('symbol',''), row.get('side',''), row.get('quantity',''), row.get('price',''), row.get('accepted',''), row.get('exit_reason') or row.get('entry_reason') or ''))
        for reason, count in data['blocks']:
            self.blocks.insert('', 'end', values=(reason, count))
        if data['signals_error']:
            self.blocks.insert('', 'end', values=(data['signals_error'], '—'))
        for widget, value in ((self.logs, data['log']), (self.console, data['console'])):
            # Preserve the user's scroll position; follow only while already at the bottom.
            follow = widget.yview()[1] >= .99
            position = widget.yview()[0]
            widget.configure(state='normal'); widget.delete('1.0', 'end'); widget.insert('1.0', value); widget.configure(state='disabled')
            widget.yview_moveto(1.0 if follow else position)

    def tick(self):
        self.status.set(self.control.status())
        self.start_button.configure(state='disabled' if self.control.running or self.closing else 'normal')
        self.stop_button.configure(state='normal' if self.control.running and not self.control.stopping else 'disabled')
        exporting = self.control.export_process and self.control.export_process.poll() is None
        self.export_button.configure(state='disabled' if exporting else 'normal')
        proc = self.control.export_process
        if proc and not exporting and self.export_seen is not proc:
            self.export_seen = proc
            messagebox.showinfo('ZIP 내보내기', '생성이 완료됐습니다. exports 폴더를 확인하세요.' if proc.returncode == 0 else '생성에 실패했습니다. 실행 / 내보내기 로그를 확인하세요.', parent=self.window)
        try:
            while True:
                day, data, error = self.results.get_nowait()
                self.loading = False
                if day != self.day.get():
                    continue
                if error:
                    for value in self.cards.values(): value.set('확인 불가')
                    for tree in (self.trades, self.blocks): tree.delete(*tree.get_children())
                    self.info.set('조회 실패: '+error)
                else:
                    self.render(data)
        except queue.Empty:
            pass
        if self.closing and not self.control.running and not exporting:
            self.window.destroy()
            return
        self.refresh()
        self.window.after(2000, self.tick)


def main():
    os.chdir(ROOT)
    window = tk.Tk()
    try:
        with single_instance_lock(ROOT / 'data/desktop.window.lock'):
            Dashboard(window)
            window.mainloop()
    except Exception as exc:
        messagebox.showerror('관리 화면 실행 실패', str(exc), parent=window)
        window.destroy()
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
