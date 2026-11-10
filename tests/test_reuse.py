"""Configuration-driven credential reuse and reviewed legacy handover."""

import contextlib
import importlib.util
import io
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
MODULE = fixtures.MODULE


class ReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='connection-reuse-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.root / '.credentials' / 'runtime.key'
        self.config_path = self.root / 'connection.local.json'
        self.fixture = self.root / 'stdio server.py'
        self.fixture.write_text(fixtures.STDIO_SERVER)
        self.server = {
            'transport': 'stdio', 'command': sys.executable,
            'args': [str(self.fixture)],
            'env': {'EVENTS': str(self.root / 'events.jsonl')},
            'env_from': {'APP_TOKEN': 'APP_TOKEN_SOURCE'},
            'runtime_key_file': '${CONFIG_DIR}/.credentials/runtime.key',
            'tunnel_id': 'tunnel_' + 'a' * 32,
        }
        self.config = {'version': 1, 'servers': {'fixture': self.server}}
        self.config_path.write_text(json.dumps(self.config))
        environment = mock.patch.dict(os.environ, {
            'APP_TOKEN_SOURCE': 'fixture-secret',
            'OPENAI_TUNNEL_KEY': 'runtime-secret',
            'KEY_TO_SAVE': 'stored-fixture-key',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = self.root / 'fake-client'
        self.client.write_text(fixtures.FAKE_CLIENT)
        self.client.chmod(0o755)
        self.data = self.root / 'managed'

    def cli(self, *args):
        return subprocess.run([sys.executable, str(MODULE), *args],
                              capture_output=True, text=True, timeout=10)

    def test_file_configuration_is_valid_without_reading_or_creating_key(self):
        connect.validate_config(self.config)
        result = self.cli('inspect', '--config', str(self.config_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['servers']['fixture']['runtime_key_file'],
                         self.server['runtime_key_file'])
        self.assertFalse(self.key.exists())

    def test_save_key_cli_requires_explicit_rotation_and_never_echoes_value(self):
        args = ('save-key', '--config', str(self.config_path), '--server', 'fixture',
                '--from-env', 'KEY_TO_SAVE')
        first = self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.key.read_text(), 'stored-fixture-key')
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key.parent.stat().st_mode & 0o777, 0o700)
        with mock.patch.dict(os.environ, {'KEY_TO_SAVE': 'rotated-fixture-key'}):
            refused = self.cli(*args)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(self.key.read_text(), 'stored-fixture-key')
            rotated = self.cli(*args, '--replace')
        self.assertEqual(rotated.returncode, 0, rotated.stderr)
        self.assertEqual(self.key.read_text(), 'rotated-fixture-key')
        for output in (first, refused, rotated):
            self.assertNotIn('stored-fixture-key', output.stdout + output.stderr)
            self.assertNotIn('rotated-fixture-key', output.stdout + output.stderr)

    def test_file_start_and_restart_reuse_key_without_prompt_or_key_environment(self):
        connect.write_runtime_key(self.key, 'stored-fixture-key')
        with mock.patch.dict(os.environ):
            os.environ.pop('OPENAI_TUNNEL_KEY', None)
            os.environ.pop('KEY_TO_SAVE', None)
            with mock.patch.object(connect.getpass, 'getpass', side_effect=AssertionError('must reuse file')):
                first = connect.start(self.client, self.data, 'fixture', self.server, self.config_path)
                self.assertTrue(first['runtime']['process_running'])
                connect.native_json(self.client, ['stop', 'fixture'], connect.native_environment(self.data))
                restarted = connect.start(self.client, self.data, 'fixture', self.server, self.config_path)
                self.assertTrue(restarted['runtime']['process_running'])
        calls = [json.loads(line) for line in (self.data / 'state/calls.jsonl').read_text().splitlines()]
        connections = [call for call in calls if call['argv'][1] == 'connect']
        self.assertEqual(len(connections), 2)
        for call in connections:
            self.assertIn('file:' + str(self.key), call['argv'])
            self.assertFalse(call['key_present'])
        self.assertNotIn('stored-fixture-key', json.dumps(calls))
        profile = (self.data / 'profiles/fixture.yaml').read_text()
        self.assertIn('file:' + str(self.key), profile)
        self.assertNotIn('stored-fixture-key', profile)
        events = [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]
        self.assertTrue(all(not event['runtime_key_leaked'] for event in events))

    def test_first_start_can_save_hidden_input_once_then_reuse_it(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('OPENAI_TUNNEL_KEY', None)
            with mock.patch.object(connect.sys.stdin, 'isatty', return_value=True), mock.patch.object(connect.getpass, 'getpass', return_value='stored-fixture-key') as prompt:
                connect.start(self.client, self.data, 'fixture', self.server, self.config_path)
                self.assertEqual(prompt.call_count, 1)
            self.assertEqual(self.key.read_text(), 'stored-fixture-key')
            connect.native_json(self.client, ['stop', 'fixture'], connect.native_environment(self.data))
            with mock.patch.object(connect.sys.stdin, 'isatty', return_value=False), mock.patch.object(connect.getpass, 'getpass', side_effect=AssertionError('no second prompt')):
                connect.start(self.client, self.data, 'fixture', self.server, self.config_path)

    def test_insecure_key_and_symlinks_are_rejected_without_connecting(self):
        connect.write_runtime_key(self.key, 'stored-fixture-key')
        self.key.chmod(0o644)
        with self.assertRaises(connect.SetupError):
            connect.start(self.client, self.data, 'fixture', self.server, self.config_path)
        self.key.chmod(0o600)
        target = self.key.with_name('target.key')
        self.key.rename(target)
        self.key.symlink_to(target)
        with self.assertRaises(connect.SetupError):
            connect.start(self.client, self.data, 'fixture', self.server, self.config_path)
        with self.assertRaises(connect.SetupError):
            connect.write_runtime_key(self.key, 'replacement-fixture-key', replace=True)
        calls = [json.loads(line) for line in (self.data / 'state/calls.jsonl').read_text().splitlines()]
        self.assertTrue(all(call['argv'][1] != 'connect' for call in calls))
        self.assertEqual(target.read_text(), 'stored-fixture-key')

    def test_file_field_rejects_malformed_paths_and_tool_key_forwarding(self):
        for value in ('', 123, 'bad\x00path'):
            with self.subTest(value=value), self.assertRaises(connect.SetupError):
                connect.validate_config({'version': 1, 'servers': {'fixture': dict(self.server, runtime_key_file=value)}})
        forwarded = dict(self.server, env_from={'APP_TOKEN': 'OPENAI_TUNNEL_KEY'})
        with self.assertRaises(connect.SetupError):
            connect.validate_config({'version': 1, 'servers': {'fixture': forwarded}})

    def start_old_runner(self):
        script = self.root / 'reviewed old runner.py'
        script.write_text("import fcntl,sys,time\nf=open(sys.argv[1],'w')\nfcntl.flock(f,fcntl.LOCK_EX)\nprint('locked',flush=True)\ntime.sleep(60)\n")
        lock = self.root / 'legacy.lock'
        process = subprocess.Popen([sys.executable, str(script), str(lock)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        self.addCleanup(cleanup)
        ready, _, _ = select.select([process.stdout], [], [], 3)
        self.assertTrue(ready)
        self.assertEqual(process.stdout.readline(), b'locked\n')
        return process, {'script': str(script), 'lock_file': str(lock), 'profile': str(self.root / 'legacy.yaml')}

    def test_real_legacy_handover_and_wrong_owner_refusal(self):
        process, legacy = self.start_old_runner()
        wrong = dict(legacy, script=str(self.root / 'different.py'))
        with self.assertRaises(connect.SetupError):
            with connect.legacy_guard(wrong):
                self.fail('must refuse the different program')
        self.assertIsNone(process.poll())
        with connect.legacy_guard(legacy) as transferred:
            self.assertTrue(transferred)
            process.wait(timeout=3)

    def test_doctor_failure_preserves_old_runner(self):
        process, legacy = self.start_old_runner()
        connect.write_runtime_key(self.key, 'stored-fixture-key')
        with mock.patch.object(connect, 'legacy_doctor', side_effect=connect.SetupError('doctor exit 2')):
            with self.assertRaises(connect.SetupError):
                connect.start(self.client, self.data, 'fixture', self.server, self.config_path, legacy=legacy)
        self.assertIsNone(process.poll())
        calls = [json.loads(line) for line in (self.data / 'state/calls.jsonl').read_text().splitlines()]
        self.assertTrue(all(call['argv'][1] != 'connect' for call in calls))

    def test_doctor_uses_a_reference_and_the_clean_new_stdio_wrapper(self):
        profile = self.root / 'legacy.yaml'
        profile.write_text('control_plane: {}\n')
        response = subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'')
        environment = connect.native_environment(self.data, self.server)
        with mock.patch.object(connect.subprocess, 'run', return_value=response) as invoked:
            connect.legacy_doctor(self.client, {'profile': str(profile), 'server': 'fixture'}, 'file:' + str(self.key),
                                  environment, self.server, self.config_path)
        arguments = invoked.call_args.args[0]
        self.assertIn('--control-plane.api-key', arguments)
        self.assertIn('file:' + str(self.key), arguments)
        self.assertIn('_stdio', arguments[arguments.index('--mcp.command') + 1])
        self.assertNotIn('stored-fixture-key', ' '.join(arguments))


if __name__ == '__main__':
    unittest.main(verbosity=2)
