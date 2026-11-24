"""T3 liveness must come from its process and listener, never a stale runtime file."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('router_t3_ops', Path(__file__).resolve().parents[1] / 'scripts/router-ops.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class T3HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.runtime = root / 'server-runtime.json'
        self.proc = root / 'proc'
        process = self.proc / '43210'
        process.mkdir(parents=True)
        (process / 'comm').write_text('t3code\n')
        self.runtime.write_text(json.dumps({'pid': 43210, 'host': '127.0.0.1', 'port': 3773}))

    def test_requires_both_live_process_and_listening_port(self):
        connection = Mock()
        with patch.object(probe.os, 'kill') as kill, patch.object(probe.socket, 'create_connection', return_value=connection) as connect:
            self.assertEqual(probe.t3_health(self.runtime, self.proc), {'status': 'healthy', 'client': 't3'})
            kill.assert_called_once_with(43210, 0)
            connect.assert_called_once_with(('127.0.0.1', 3773), timeout=2)
            connection.close.assert_called_once()

    def test_stale_pid_is_down_without_connecting(self):
        with patch.object(probe.os, 'kill', side_effect=ProcessLookupError), patch.object(probe.socket, 'create_connection') as connect:
            self.assertEqual(probe.t3_health(self.runtime, self.proc)['status'], 'down')
            connect.assert_not_called()

    def test_closed_listener_is_down(self):
        with patch.object(probe.os, 'kill'), patch.object(probe.socket, 'create_connection', side_effect=ConnectionRefusedError):
            self.assertEqual(probe.t3_health(self.runtime, self.proc)['status'], 'down')

    def test_reused_pid_and_remote_targets_are_not_healthy(self):
        with patch.object(probe.os, 'kill') as kill, patch.object(probe.socket, 'create_connection') as connect:
            (self.proc / '43210/comm').write_text('unrelated\n')
            self.assertEqual(probe.t3_health(self.runtime, self.proc)['status'], 'unknown')
            self.runtime.write_text(json.dumps({'pid': 43210, 'host': 'example.com', 'port': 3773}))
            self.assertEqual(probe.t3_health(self.runtime, self.proc)['status'], 'unknown')
            kill.assert_not_called()
            connect.assert_not_called()

    def test_no_t3_installation_keeps_legacy_probe_available(self):
        self.runtime.unlink()
        self.assertIsNone(probe.t3_health(self.runtime, self.proc))
