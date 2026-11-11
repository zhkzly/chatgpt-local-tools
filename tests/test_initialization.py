"""Regression coverage for stdio notification compatibility and orphan clients."""

import contextlib
import ctypes
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_connect as fixtures


connect = fixtures.connect


class InitializationTests(unittest.TestCase):
    def test_notification_setting_is_typed_and_stdio_only(self):
        for value in (True, False):
            server = {'transport': 'stdio', 'command': sys.executable,
                      'stdio_send_initialized_notification': value}
            connect.validate_config({'version': 1, 'servers': {'demo': server}})
        for value in ('true', 1, None):
            with self.subTest(value=value), self.assertRaises(connect.SetupError):
                connect.validate_config({'version': 1, 'servers': {'demo': {
                    'transport': 'stdio', 'command': sys.executable,
                    'stdio_send_initialized_notification': value}}})
        with self.assertRaises(connect.SetupError):
            connect.validate_config({'version': 1, 'servers': {'demo': {
                'transport': 'http', 'url': 'http://127.0.0.1:1234/mcp',
                'stdio_send_initialized_notification': True}}})

    def test_notification_reaches_client_but_not_mcp_or_unrelated_connections(self):
        server = {'transport': 'stdio', 'command': sys.executable,
                  'stdio_send_initialized_notification': True}
        with mock.patch.dict(os.environ, {'MCP_STDIO_SEND_INITIALIZED_NOTIFICATION': 'true'}):
            enabled = connect.native_environment(Path('/tmp/fixture'), server)
            self.assertEqual(enabled['MCP_STDIO_SEND_INITIALIZED_NOTIFICATION'], 'true')
            self.assertNotIn('MCP_STDIO_SEND_INITIALIZED_NOTIFICATION',
                             connect.process_environment(server, Path('/tmp/config.json')))
            self.assertNotIn('MCP_STDIO_SEND_INITIALIZED_NOTIFICATION',
                             connect.native_environment(Path('/tmp/fixture'),
                                                        {'transport': 'stdio', 'command': sys.executable}))
        disabled = connect.native_environment(Path('/tmp/fixture'), dict(server, stdio_send_initialized_notification=False))
        self.assertEqual(disabled['MCP_STDIO_SEND_INITIALIZED_NOTIFICATION'], 'false')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Requires the Linux process inventory')
class OrphanClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='orphan-tunnel-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / 'profiles/legacy.yaml'
        self.profile.parent.mkdir()
        self.profile.write_text('control_plane: {}\n')
        self.lock = self.root / 'legacy.lock'
        self.legacy = {'script': str(self.root / 'old.py'), 'lock_file': str(self.lock),
                       'profile': str(self.profile)}

    def client_shaped_process(self, flags):
        # This fixture only holds a client-shaped command line. Detection must
        # refuse a conflict for review, never use this as authority to signal it.
        executable = self.root / 'tunnel-client-fixture'
        if not executable.exists():
            executable.symlink_to(sys.executable)
        script = self.root / 'run'
        script.write_text("import ctypes,time\nlibc=ctypes.CDLL(None)\nlibc.prctl(15,b'tunnel-client',0,0,0)\nprint('ready',flush=True)\ntime.sleep(60)\n")
        process = subprocess.Popen([str(executable), 'run', *flags], cwd=self.root,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
            process.stdout.close()
        self.addCleanup(cleanup)
        readable, _, _ = select.select([process.stdout], [], [], 3)
        self.assertTrue(readable)
        self.assertEqual(process.stdout.readline(), b'ready\n')
        return process

    def assert_conflict_is_not_signalled(self, process):
        with self.assertRaises(connect.SetupError) as caught:
            with connect.legacy_guard(self.legacy):
                self.fail('must not start another runtime while the old profile is active')
        self.assertIn(str(process.pid), str(caught.exception))
        self.assertIsNone(process.poll(), 'Conflict detection must not signal the process')

    def test_free_python_lock_does_not_hide_a_surviving_client(self):
        self.lock.touch()
        process = self.client_shaped_process(['--profile', 'legacy', '--profile-dir', str(self.profile.parent)])
        self.assert_conflict_is_not_signalled(process)

    def test_missing_python_lock_does_not_hide_a_surviving_client(self):
        process = self.client_shaped_process(['--profile=legacy', '--profile-dir=' + str(self.profile.parent)])
        self.assert_conflict_is_not_signalled(process)

    def test_explicit_profile_file_form_is_detected(self):
        process = self.client_shaped_process(['--profile-file', str(self.profile)])
        self.assert_conflict_is_not_signalled(process)

    def test_other_profile_is_not_stopped_or_blocked(self):
        self.lock.touch()
        process = self.client_shaped_process(['--profile', 'unrelated', '--profile-dir', str(self.profile.parent)])
        with connect.legacy_guard(self.legacy) as transferred:
            self.assertFalse(transferred)
        self.assertIsNone(process.poll())


if __name__ == '__main__':
    unittest.main(verbosity=2)
