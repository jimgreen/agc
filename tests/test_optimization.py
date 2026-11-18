"""Regression tests use temporary SQLite databases and fake SCADA only."""
import importlib
import logging
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agc_ctrl
from agc_db import AGCDataStore
from web import server


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.sender = Mock(return_value=True)
        self.fes = agc_ctrl.FesClient(lambda *a: None, lambda *a: None,
            send_yk_value=self.sender, send_yt_value=self.sender, source='test')
        with patch.object(agc_ctrl, '_load_fes_client', return_value=self.fes), patch.object(
            agc_ctrl.SystemConfig, '_setup_logger', return_value=logging.Logger('isolated-test')):
            self.state = server.AGCState(self.path / 'test.db', self.path, start_worker=False)
        self.addCleanup(self.state.close)
        self.device = self.state.model.wind_generator[0]
        self.kind, self.id = 'wind', str(self.device.id)

    def save(self, **parameters):
        return self.state.device_config(dict(kind=self.kind, id=self.id, parameters=parameters))

    def test_import_has_no_runtime_state(self):
        self.assertIsNone(importlib.import_module('web.server').STATE)

    def test_invalid_fields_do_not_partially_save(self):
        before = self.device.p_step
        with self.assertRaises(ValueError):
            self.save(control_step=before + 1, run_stat=1.5)
        self.assertEqual(self.device.p_step, before)
        self.assertEqual(self.state.db.device_rows()[0]['control_state'], 'remote')

    def test_rejects_nonfinite_and_invalid_values(self):
        for field, values in {'run_stat': [-1, 2, 0.5, float('inf')],
            'control_step': [-1, 'bad', float('nan')], 'preset_power': [999999, float('nan')]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.save(**{field: value})

    def test_curve_requires_96_finite_in_range_points(self):
        for curve in [[1] * 95, [float('nan')] * 96, [999999] * 96]:
            with self.subTest(curve=curve[:1]), self.assertRaises(ValueError):
                self.save(preset_curve=curve)

    def test_parameter_save_preserves_curve_and_no_run_command(self):
        curve = [float(i % 5) for i in range(96)]
        self.save(preset_curve=curve)
        self.device.run_yk_rtu, self.device.run_yk_pnt = 1, 1
        self.save(control_step=0.5, preset_power=self.device.agc_preset_power, run_stat=self.device.run_stat)
        self.assertEqual(self.state.db.curve(self.kind, self.id), curve)
        self.sender.assert_not_called()

    def test_single_device_save_does_not_rewrite_curves(self):
        sql = []
        self.state.db.conn.set_trace_callback(sql.append)
        self.save(control_step=0.5)
        self.assertFalse(any('DELETE FROM device_curves' in line for line in sql))
        self.assertEqual(sum('INSERT INTO devices' in line for line in sql), 1)

    def test_curve_and_device_rollback_on_db_failure(self):
        before = dict(vars(self.device))
        with patch.object(self.state.db, 'save_curve', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                self.save(preset_curve=[2.0] * 96)
        self.assertEqual(vars(self.device), before)
        row = self.state.db.fetchone('SELECT preset_power FROM devices WHERE kind=? AND device_id=?', (self.kind, self.id))
        self.assertEqual(row['preset_power'], before['agc_preset_power'])

    def test_mode_and_local_config_survive_old_cycle(self):
        self.state.db.save_cycle(dict(cycle=1, controlMode='closed_loop', commands=[dict(kind=self.kind, id=self.id, controlState='remote')]))
        self.state.set_control_mode('open_loop')
        self.save(control_state='local')
        snapshot = self.state._snapshot()
        self.assertEqual(snapshot['controlMode'], 'open_loop')
        self.assertEqual(self.device.agc_control_state, 'local')

    def test_local_realtime_is_not_command(self):
        self.device.agc_control_state = 'local'
        self.device.p_cur, self.device.p_ctrl = 3.0, 7.0
        self.assertEqual(self.state._device(self.kind, self.device)['power'], 3.0)

    def test_lite_history_initial_and_new_cycle(self):
        first = self.state._snapshot(False, False, history_since=-1)
        self.assertIn('history', first)
        self.assertNotIn('presetCurve', first['devices'][0])
        same = self.state._snapshot(False, False, history_since=first['cycle'])
        self.assertNotIn('history', same)
        self.state.db.save_cycle({'cycle': 1})
        fresh = self.state._snapshot(False, False, history_since=0)
        self.assertEqual(len(fresh['history']), 1)

    def test_unchanged_snapshot_skips_history_scan(self):
        self.state.db.save_cycle({'cycle': 1})
        self.state._snapshot()
        with patch.object(self.state.db, 'history', wraps=self.state.db.history) as query:
            for _ in range(5): self.state._snapshot(False, False)
        query.assert_not_called()

    def test_new_cycle_does_not_replace_computed_values(self):
        self.state.db.save_cycle(dict(cycle=7, commands=[dict(kind=self.kind, id=self.id, realtimePower=1, commandPower=1)]))
        self.state.do_one_ctrl = lambda *_: setattr(self.device, 'p_ctrl', 6.0)
        point = self.state.run_cycle()
        self.assertEqual(point['cycle'], 8)
        saved = self.state.db.commands(self.kind, self.id, 1)[0]
        self.assertEqual(saved['commandPower'], 6.0)

    def test_failed_remote_control_preserves_state(self):
        self.device.run_yk_rtu, self.device.run_yk_pnt = 1, 1
        self.sender.return_value = False
        before = self.device.run_stat
        with self.assertRaises(ValueError): self.save(run_stat=1 - before)
        self.assertEqual(self.device.run_stat, before)

    def test_storage_negative_curve_and_bounds(self):
        self.kind, self.id = 'storage', str(self.state.model.estorage[0].id)
        self.save(preset_curve=[-1.0] * 96)
        self.assertEqual(self.state.db.curve(self.kind, self.id), [-1.0] * 96)
        data = self.state.device_detail(self.kind, self.id)['device']
        self.assertLess(data['pMin'], 0)

    def test_local_curve_respects_step_and_deadband(self):
        self.device.agc_control_state = 'local'
        self.device.agc_preset_curve = [9.0] * 96
        self.device.p_ctrl = 2.0
        self.device.agc_control_step = 1.0
        self.device.agc_control_deadband = 0.1
        self.state._apply_local_curves()
        self.assertEqual(self.device.p_ctrl, 3.0)
        self.assertEqual(self.device.agc_preset_power, 9.0)

    def test_local_stopped_device_is_zero(self):
        self.device.agc_control_state = 'local'
        self.device.agc_preset_curve = [9.0] * 96
        self.device.run_stat = 0
        self.state._apply_local_curves()
        self.assertEqual(self.device.p_ctrl, 0.0)

    def test_local_storage_cannot_charge_at_soc_max(self):
        device = self.state.model.estorage[0]
        device.agc_control_state = 'local'
        device.run_stat = 1
        device.agc_preset_curve = [-2.0] * 96
        device.soc_cur = device.soc_max
        device.p_ctrl = 0.0
        self.state._apply_local_curves()
        self.assertEqual(device.p_ctrl, 0.0)
        self.assertEqual(device.agc_preset_power, -2.0)

    def test_cycle_history_contains_summary_only(self):
        self.state.run_cycle()
        point = self.state._snapshot()['history'][-1]
        self.assertIn('summary', point)
        self.assertNotIn('devices', point)
        self.assertNotIn('commands', point)

    def test_http_state_and_invalid_device_return_json(self):
        import json
        from urllib.error import HTTPError
        from urllib.request import urlopen
        httpd = server.AGCHTTPServer(('127.0.0.1', 0), server.HMIHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with patch.object(server, 'STATE', self.state):
            thread.start()
            try:
                base = f'http://127.0.0.1:{httpd.server_address[1]}'
                with urlopen(base + '/api/state?lite=1&history_since=-1') as response:
                    self.assertIn('history', json.load(response))
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base + '/api/device-detail?kind=wind&id=missing')
                self.assertEqual(caught.exception.code, 400)
                self.assertIn('error', json.load(caught.exception))
            finally:
                httpd.shutdown()
                thread.join()
                httpd.server_close()

    def test_same_port_cannot_bind_twice(self):
        httpd = server.AGCHTTPServer(('127.0.0.1', 0), server.HMIHandler)
        self.addCleanup(httpd.server_close)
        with self.assertRaises(OSError):
            server.AGCHTTPServer(httpd.server_address, server.HMIHandler)

    def test_shared_database_cycle_writers_do_not_overwrite(self):
        other = AGCDataStore(self.path / 'test.db')
        self.addCleanup(other.close)
        def web_write():
            for _ in range(20): self.state.db.save_cycle({'cycle': 1, 'commands': [{'kind':'wind', 'id':'1'}]})
        def calc_write():
            for _ in range(20): other.record_cycle(self.state.model, self.state.config)
        with ThreadPoolExecutor(2) as pool:
            results = [pool.submit(web_write), pool.submit(calc_write)]
            for result in results: result.result()
        rows = self.state.db.fetchall('SELECT cycle FROM control_cycles ORDER BY cycle')
        self.assertEqual([row['cycle'] for row in rows], list(range(1, 41)))
        self.assertEqual(self.state.db.fetchone('SELECT COUNT(DISTINCT cycle) AS n FROM device_samples')['n'], 40)

    def test_cycle_and_samples_rollback_together(self):
        with patch.object(self.state.db, 'executemany', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                self.state.db.save_cycle(dict(cycle=1, commands=[{'kind':'wind', 'id':1}]))
        self.assertIsNone(self.state.db.latest_cycle_row())


class ScadaTests(unittest.TestCase):
    def setUp(self):
        self.send = Mock(return_value=True)
        with patch.object(agc_ctrl.SystemConfig, '_setup_logger', return_value=logging.Logger('fake-scada')):
            self.config = agc_ctrl.SystemConfig(SimpleNamespace(), fes_client=agc_ctrl.FesClient(
                lambda *a: None, lambda *a: None, send_yt_value=self.send, send_yk_value=self.send))

    def test_yt_dedupe_only_current_gather_cycle(self):
        for _ in range(2): self.assertTrue(self.config._send_scada_command('yt', 1, 1, 3))
        self.assertEqual(self.send.call_count, 1)
        agc_ctrl.data_gather(self.config)
        self.assertTrue(self.config._send_scada_command('yt', 1, 1, 3))
        self.assertEqual(self.send.call_count, 2)

    def test_failed_change_invalidates_previous_success(self):
        self.config._send_scada_command('yt', 1, 1, 3)
        self.send.return_value = False
        self.assertFalse(self.config._send_scada_command('yt', 1, 1, 4))
        self.send.return_value = True
        self.config._send_scada_command('yt', 1, 1, 3)
        self.assertEqual(self.send.call_count, 3)

    def test_yk_never_suppresses_explicit_action(self):
        for _ in range(2): self.config._send_scada_command('yk', 1, 1, 1)
        self.assertEqual(self.send.call_count, 2)

    def test_open_loop_blocks_and_invalidates_cache(self):
        self.config._send_scada_command('yt', 1, 1, 3)
        self.config.control_mode = 'open_loop'
        self.assertFalse(self.config._send_scada_command('yt', 1, 1, 3))
        self.config.control_mode = 'closed_loop'
        self.config._send_scada_command('yt', 1, 1, 3)
        self.assertEqual(self.send.call_count, 2)

    def test_command_audit_memory_is_bounded(self):
        for i in range(1100): self.config._send_scada_command('yt', 1, 1, 3)
        self.assertEqual(len(self.config.command_results), 1000)

    def test_read_signatures_and_write_signatures(self):
        funcs = [(lambda a,b: (a,b), (1,2)), (lambda a,b,c,d: (a,b,c,d), ('s','c',1,2))]
        for fn, expected in funcs:
            self.assertEqual(agc_ctrl._invoke_scada_callable(fn,'s','c',1,2), expected)
        self.assertEqual(agc_ctrl._invoke_scada_callable(lambda a,b,c: c, None,None,1,2,0), 0)

    def test_internal_typeerror_is_not_retried(self):
        calls=[]
        def broken(a,b):
            calls.append(1)
            raise TypeError('internal')
        with self.assertRaises(TypeError): agc_ctrl._invoke_scada_callable(broken,None,None,1,2)
        self.assertEqual(len(calls),1)

    def test_unhashable_callable(self):
        class Reader:
            __hash__ = None
            def __call__(self,a,b): return a+b
        self.assertEqual(agc_ctrl._invoke_scada_callable(Reader(),None,None,1,2),3)

    def test_log_limit_and_recovery(self):
        self.config.logger = Mock()
        for i in range(50): self.config.log_scada_issue(logging.INFO,'yc','name',1,1,str(i))
        self.assertEqual(len(self.config._scada_issue_logged_at),1)
        self.assertEqual(self.config.logger.log.call_count,1)
        self.config.clear_scada_issue(('yc',),1,1)
        self.config.log_scada_issue(logging.INFO,'yc','name',1,1,'failure again')
        self.assertEqual(self.config.logger.log.call_count,2)

if __name__ == '__main__':
    unittest.main()
