"""Local browser fixture: temporary database, fake SCADA, no plant I/O."""
import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agc_ctrl
from web import server


def main():
    with tempfile.TemporaryDirectory(prefix='agc-browser-') as folder:
        root = Path(folder)
        fake = agc_ctrl.FesClient(lambda *a: None, lambda *a: None,
            send_yt_value=lambda *a: True, send_yk_value=lambda *a: True, source='isolated-fixture')
        with patch.object(agc_ctrl, '_load_fes_client', return_value=fake), patch.object(
            agc_ctrl.SystemConfig, '_setup_logger', return_value=logging.Logger('browser-fixture')):
            server.STATE = server.AGCState(root / 'fixture.db', root, start_worker=False)
        state = server.STATE
        for i in range(150):
            point = state._snapshot(False, False, sync_database=False)
            state.cycle = i + 1
            point['cycle'] = state.cycle
            point['commands'] = state._record_commands(point['timestamp'])
            state.db.save_cycle(point)
        httpd = server.AGCHTTPServer(('127.0.0.1', 18766), server.HMIHandler)
        print('isolated fixture http://127.0.0.1:18766/', flush=True)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
            state.close()


if __name__ == '__main__':
    main()
