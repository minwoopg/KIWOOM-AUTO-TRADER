"""Offline desktop control/data tests. No broker login or real orders."""
import asyncio
import csv
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.desktop_control import STOP_FILE_ENV, sleep_or_stop, cleanup_stop_record
from app.desktop_model import ProcessController, csv_rows, snapshot, on_day, read_tail
from app.main import trading_loop
from domain.cost_model import CostModel
from test_run_once_integration import build_minimal_settings
from infra.storage.process_lock import single_instance_lock


class ControlTests(unittest.TestCase):
    def test_preexisting_stop_interrupts_wait(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'stop'; p.touch()
            with patch.dict(os.environ, {STOP_FILE_ENV: str(p)}):
                self.assertTrue(asyncio.run(sleep_or_stop(100)))

    def test_stop_arriving_during_wait(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'stop'
            async def scenario():
                async def stop():
                    await asyncio.sleep(.01); p.touch()
                task = asyncio.create_task(stop())
                result = await asyncio.wait_for(sleep_or_stop(3600), 2)
                await task
                return result
            with patch.dict(os.environ, {STOP_FILE_ENV: str(p)}):
                self.assertTrue(asyncio.run(scenario()))

    def test_legacy_sleep_is_unchanged(self):
        with patch.dict(os.environ, {}, clear=True), patch('app.desktop_control.asyncio.sleep', new_callable=AsyncMock) as sleep:
            self.assertFalse(asyncio.run(sleep_or_stop(10)))
            sleep.assert_awaited_once_with(10)

    def test_premarket_stop_never_calls_trading_service(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'stop'; p.touch()
            service = Mock()
            with patch.dict(os.environ, {STOP_FILE_ENV: str(p)}), patch('app.main.seconds_until_market_open', return_value=3600):
                asyncio.run(trading_loop(service, build_minimal_settings(d), Mock()))
            service.run_once.assert_not_called()

    def test_stop_waits_for_cycle_to_complete(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'stop'
            events = []
            async def cycle():
                events.append('start'); p.touch()
                await asyncio.sleep(.01)
                events.append('completed')
            service = Mock()
            service.run_once = AsyncMock(side_effect=cycle)
            # The loop explicitly checks this predicate before run_once.
            service.is_in_balance_outage.return_value = False
            service.in_balance_outage = False
            with patch.dict(os.environ, {STOP_FILE_ENV: str(p)}), patch('app.main.seconds_until_market_open', return_value=0), patch('app.main.is_market_open', return_value=True):
                asyncio.run(asyncio.wait_for(trading_loop(service, build_minimal_settings(d), Mock()), 3))
            self.assertEqual(events, ['start', 'completed'])
            service.run_once.assert_awaited_once()

    def test_start_owned_stop_duplicate_and_exit_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            controller = ProcessController(d)
            proc = Mock(pid=123, returncode=None)
            proc.poll.return_value = None
            with patch('app.desktop_model.subprocess.Popen', return_value=proc) as spawn:
                controller.start()
                args, kw = spawn.call_args
                self.assertEqual(args[0][-2:], ['-m', 'app.main'])
                self.assertEqual(kw['cwd'], Path(d))
                self.assertEqual(kw['env'][STOP_FILE_ENV], str(controller.stop_file))
                self.assertFalse(controller.stop_file.exists())
                with self.assertRaises(RuntimeError): controller.start()
                controller.request_stop()
                self.assertTrue(controller.stop_file.exists())
                self.assertIn('종료 요청 중', controller.status())
                proc.terminate.assert_not_called(); proc.kill.assert_not_called()
                proc.poll.return_value = 0; proc.returncode = 0
                self.assertIn('종료 코드 0', controller.status())
                self.assertFalse(controller.stop_file.exists())
                self.assertFalse(controller.stop_file.with_suffix('.owner').exists())

    def test_recovery_targets_only_gui_owner_records(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)/'data/desktop'; root.mkdir(parents=True)
            token = root/('a'*32+'.owner'); token.touch()
            (root/'unrelated.owner').touch()
            self.assertEqual(ProcessController(d).request_previous_stops(), 1)
            self.assertTrue(token.with_suffix('.stop').exists())
            self.assertFalse((root/'unrelated.stop').exists())
            with patch.dict(os.environ, {STOP_FILE_ENV: str(token.with_suffix('.stop'))}):
                cleanup_stop_record()
            self.assertFalse(token.exists())

    def test_export_rejects_invalid_date_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            controller=ProcessController(d)
            with self.assertRaises(ValueError): controller.export('2026-99-99')
            proc=Mock(); proc.poll.return_value=None
            with patch('app.desktop_model.subprocess.Popen', return_value=proc) as spawn:
                controller.export('2026-10-13')
                self.assertEqual(spawn.call_args.args[0][-2:], ['export_daily_bundle.py','2026-10-13'])
                with self.assertRaises(RuntimeError): controller.export('2026-10-13')

    def test_existing_lock_still_excludes_second_process(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.lock'
            with single_instance_lock(path):
                command='from infra.storage.process_lock import single_instance_lock; import sys\nwith single_instance_lock(sys.argv[1]): pass'
                result=subprocess.run([sys.executable,'-c',command,str(path)],capture_output=True)
                self.assertNotEqual(result.returncode,0)


class DataTests(unittest.TestCase):
    def test_csv_partial_and_oversize_are_not_zero_activity(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.csv'; p.write_text('timestamp,symbol\n2026-10-13,005930')
            self.assertTrue(csv_rows(p,1000)[1])
            p.write_text('timestamp,symbol\n2026-10-13,005930\n')
            self.assertTrue(csv_rows(p,5)[1])
            self.assertEqual(len(csv_rows(p,1000)[0]),1)

    def test_tail_is_explicitly_partial_and_empty_file_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'signals.csv'
            p.write_text('timestamp,symbol\n'+''.join(f'2026-10-13T10:00:00,00{i:04d}\n' for i in range(100)))
            rows, notice=csv_rows(p,200,allow_tail=True)
            self.assertTrue(rows)
            self.assertLess(len(rows),100)
            self.assertIn('전체 일일 건수 아님',notice)
            p.write_text('')
            self.assertTrue(csv_rows(p,200)[1])

    def test_korea_date_boundary(self):
        self.assertTrue(on_day({'timestamp':'2026-10-12T15:01:00+00:00'},'2026-10-13'))
        self.assertFalse(on_day({'timestamp':'invalid'},'2026-10-13'))

    def test_snapshot_does_not_present_accepted_as_filled(self):
        with tempfile.TemporaryDirectory() as d:
            settings=build_minimal_settings(d)
            path=Path(settings.storage.trade_log_file); path.parent.mkdir(parents=True,exist_ok=True)
            fields=['timestamp','symbol','side','price','quantity','accepted']
            with path.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader()
                writer.writerow(dict(timestamp='2026-10-13T10:00:00',symbol='005930',side='BUY',price=10000,quantity=10,accepted='True'))
                writer.writerow(dict(timestamp='2026-10-13T10:05:00',symbol='005930',side='SELL',price=10100,quantity=10,accepted='True'))
            with patch('app.desktop_model.load_settings',return_value=settings),patch('app.desktop_model.load_cost_model',return_value=CostModel()):
                data=snapshot(Path(d),'2026-10-13')
            self.assertEqual(data['gross'],1000)
            self.assertEqual(data['base'],650)
            self.assertEqual(data['pairs'],1)
            self.assertTrue(data['signals_error'])
            self.assertIn('확인 불가',data['state_note'])
            path.unlink()
            with patch('app.desktop_model.load_settings',return_value=settings),patch('app.desktop_model.load_cost_model',return_value=CostModel()):
                data=snapshot(Path(d),'2026-10-13')
            self.assertIsNone(data['gross'])

    def test_display_log_masks_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'app.log';p.write_text('appkey=SECRET_EXAMPLE\n')
            self.assertNotIn('SECRET_EXAMPLE',read_tail(p))


if __name__=='__main__':
    unittest.main()
