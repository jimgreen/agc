"""Time-range queries and read-only analysis, isolated from runtime data."""
import logging, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from web import server
import agc_ctrl

class HistoryQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db=server.AGCDatabase(Path(self.temp.name)/'query.db'); self.addCleanup(self.db.close)
        for i in range(1,8):
            self.db.save_cycle({'cycle':i,'timestamp':f'2026-11-18T0{i}:00:00+00:00',
                'summary':{'load':i*10,'renewable':i,'balance':-i/10}})

    def test_range_is_start_inclusive_end_exclusive(self):
        result=self.db.query_history('2026-11-18T15:00:00+13:00','2026-11-18T17:00:00+13:00')
        self.assertEqual([p['cycle'] for p in result['items']],[2,3])
        self.assertEqual(result['statistics']['avgLoad'],25)

    def test_cursor_stays_stable_when_new_cycle_arrives(self):
        first=self.db.query_history(limit=3)
        self.db.save_cycle({'cycle':8,'timestamp':'2026-11-18T08:00:00+00:00'})
        second=self.db.query_history(anchor=first['anchor'],before=first['nextBefore'],limit=3)
        third=self.db.query_history(anchor=first['anchor'],before=second['nextBefore'],limit=3)
        self.assertEqual(first['statistics']['count'],7)
        self.assertEqual(second['statistics']['count'],7)
        self.assertEqual([p['cycle'] for page in [third,second,first] for p in page['items']],list(range(1,8)))
        self.assertFalse(third['hasMore'])

    def test_invalid_and_reversed_time_rejected(self):
        for start,end in [('bad',''),('2026-11-18T01:00',''),('2026-11-18T07:00Z','2026-11-18T01:00Z')]:
            with self.subTest(start=start),self.assertRaises(ValueError): self.db.query_history(start,end)

    def test_empty_range_has_zero_count(self):
        result=self.db.query_history('2030-01-01T00:00Z')
        self.assertEqual(result['items'],[]); self.assertEqual(result['statistics']['count'],0)
        self.assertIsNone(result['statistics']['avgLoad'])

    def test_export_limit_marks_remaining_rows(self):
        result=self.db.query_history(limit=2)
        self.assertEqual(len(result['items']),2); self.assertTrue(result['hasMore'])
        self.assertEqual(result['limit'],2)

    def test_range_statistics_cover_more_than_page(self):
        result=self.db.query_history(limit=1)
        self.assertEqual(result['statistics']['peakLoad'],70)
        self.assertEqual(result['statistics']['count'],7)
        self.assertAlmostEqual(result['statistics']['maxAbsBalance'],0.7)

    def test_offset_times_are_ordered_by_actual_instant(self):
        self.db.save_cycle({'cycle':8,'timestamp':'2026-11-18T13:30:00+13:00'})
        result=self.db.query_history(end='2026-11-18T01:00:00Z')
        self.assertEqual([p['cycle'] for p in result['items']],[8])

class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.sender=unittest.mock.Mock(return_value=True)
        self.fes=agc_ctrl.FesClient(lambda *a: None,lambda *a: None,
            send_yk_value=self.sender,send_yt_value=self.sender,source='test')
        with patch.object(agc_ctrl,'_load_fes_client',return_value=self.fes), patch.object(
            agc_ctrl.SystemConfig,'_setup_logger',return_value=logging.Logger('round2-analysis')):
            self.state=server.AGCState(self.path/'test.db',self.path,start_worker=False)
        self.addCleanup(self.state.close)
        self.device=self.state.model.wind_generator[0]

    def test_preview_never_sends_or_replaces_decision(self):
        self.state.model.agc_renewable_acceptance={'previous':True}
        before=[getattr(d,'p_ctrl',None) for d in self.state._all_devices()]
        result=self.state.analysis_payload()
        self.sender.assert_not_called()
        self.assertEqual(before,[getattr(d,'p_ctrl',None) for d in self.state._all_devices()])
        self.assertEqual(self.state.model.agc_renewable_acceptance,{'previous':True})
        self.assertEqual(len(result['devices']),28)

    def test_conditions_reflect_local_stopped_and_power_limit(self):
        self.device.agc_control_state='local'
        self.assertEqual(self.state.analysis_payload()['devices'][0]['reason'],'本地曲线')
        self.device.run_stat=0
        self.assertEqual(self.state.analysis_payload()['devices'][0]['reason'],'停运')
        self.device.run_stat=1; self.device.agc_control_state='remote'; self.device.p_cur=self.device.p_max
        self.assertEqual(self.state.analysis_payload()['devices'][0]['reason'],'已达功率上限')


if __name__=='__main__': unittest.main()
