"""Account rotation uses only isolated, synthetic profiles and receipts."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._platform import symlink_or_skip
from tests.claude_profile_sync_fixtures import fingerprint, load_helper
from tests.install_fixtures import install_mod
from tests.test_claude_desktop_config import _profile, _state, desktop_config


rotation = load_helper('claude_profile_rotation')
receipts = install_mod._codex_config
CRASH = r'''
import importlib.util, json, os, pathlib, sys
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
helper = pathlib.Path(sys.argv[1])
rotation = load('rotation_worker', helper)
receipts = load('receipts_worker', helper.with_name('codex_config.py'))
receipts._atomic_write = rotation._atomic
home, profile, boundary = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4]
if boundary.startswith('move-'):
    original = rotation._publish.move_new
    count = 0
    def move(source, target):
        global count
        if '.tmp-' in source.name and target != home / rotation.PENDING:
            return original(source, target)
        count += 1
        if boundary == 'move-before-' + str(count): os._exit(73)
        original(source, target)
        if boundary == 'move-after-' + str(count): os._exit(73)
    rotation._publish.move_new = move
elif boundary.startswith(('control-', 'finish-')):
    original = rotation._atomic.remove
    count = 0
    def remove(path, **kwargs):
        global count
        label = 'finish'
        if path.name in (rotation.ACCOUNTS, rotation.LEDGER):
            count += 1
            label = 'control-' + str(count)
        if boundary == label + '-before': os._exit(73)
        original(path, **kwargs)
        if boundary == label + '-after': os._exit(73)
    rotation._atomic.remove = remove
elif boundary.startswith('receipt-'):
    original = receipts.replace_receipt
    def replace(*args, **kwargs):
        if boundary == 'receipt-before': os._exit(73)
        original(*args, **kwargs)
        if boundary == 'receipt-after': os._exit(73)
    receipts.replace_receipt = replace
rotation.rotate(home, profile, json.loads((home / rotation.STATE).read_text()), receipts)
'''



class RotationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'router'
        self.home.mkdir(mode=0o700)
        self.profile = _profile(self.root, {'vendorSetting': 'preserve'})
        self.state = _state()
        (self.home / rotation.STATE).write_text(json.dumps(self.state))
        desktop_config.attach(self.home, self.state, receipts, data_dir_override=self.profile)
        receipts.write_client_receipt(self.home, 'codex', {'keep': ['another', 'client']})
        self.receipt_path = self.home / 'activation.json'
        self.receipt_before = self.receipt_path.read_bytes()
        self.binding = self.home / rotation.ACCOUNTS
        self.binding.write_text(json.dumps({'accounts': 'a' * 64}))
        self.ledger = self.home / rotation.LEDGER
        self.ledger.write_bytes(b'{"version":1,"stores":{"synthetic":"keep"}}\n')
        (self.profile / 'ant-did').write_bytes(b'old-synthetic-device')
        (self.profile / 'config.json').write_text('{"account":"old-synthetic"}')
        (self.profile / '.claude-profile-control-owner.json').write_text('{"owner":"old"}')
        task = self.profile / 'claude-code-sessions' / 'old' / 'org' / 'local_task.json'
        task.parent.mkdir(parents=True)
        task.write_text('{"title":"old task"}')
        transcript = task.parent / 'local_task' / '.claude' / 'projects' / 'old.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text('synthetic transcript\n')
        self.before = fingerprint(self.profile)
        self.controls = {name: (self.home / name).read_bytes()
                         for name in (rotation.ACCOUNTS, rotation.LEDGER)}

    def rotate(self):
        return rotation.rotate(self.home, self.profile, self.state, receipts)

    def archives(self):
        return sorted(self.root.glob('.Claude-3p.rotation-*.archive'))

    def assert_original(self):
        self.assertEqual(fingerprint(self.profile), self.before)
        self.assertEqual(self.receipt_path.read_bytes(), self.receipt_before)
        for name, before in self.controls.items():
            self.assertEqual((self.home / name).read_bytes(), before)
        self.assertFalse(rotation.pending(self.home))

    def test_rotation_preserves_full_old_profile_and_builds_only_clean_configuration(self):
        path = self.rotate()
        archive, = self.archives()
        self.assertEqual(fingerprint(archive / 'profile'), self.before)
        self.assertEqual((archive / 'activation.json').read_bytes(), self.receipt_before)
        for name, before in self.controls.items():
            self.assertEqual((archive / name).read_bytes(), before)
            self.assertEqual((archive / name).read_bytes(), before)
            self.assertFalse((self.home / name).exists())
        self.assertFalse(rotation.pending(self.home))
        self.assertEqual(path.parent, self.profile / 'configLibrary')
        self.assertEqual(set(p.name for p in self.profile.iterdir()),
                         {'configLibrary', 'claude_desktop_config.json'})
        self.assertEqual(json.loads(path.read_bytes()), desktop_config.desired_values(self.state))
        self.assertEqual(json.loads((self.profile / 'claude_desktop_config.json').read_bytes()),
                         {})
        after = json.loads(self.receipt_path.read_bytes())
        prior = json.loads(self.receipt_before)
        self.assertEqual(after['clients']['codex'], prior['clients']['codex'])
        self.assertEqual(after['clients']['claude-desktop']['config_path'], str(path))
        self.assertNotEqual(path.name, Path(prior['clients']['claude-desktop']['config_path']).name)
        self.assertEqual(json.loads((path.parent / '_meta.json').read_bytes())['appliedId'], path.stem)
        self.assertTrue((archive / 'rotation.json').is_file())
        self.assertEqual(rotation.recover(self.home, receipts), 'none')

    def test_missing_binding_is_allowed_but_retained_ledger_is_not_discarded(self):
        self.binding.unlink()
        self.rotate()
        archive, = self.archives()
        self.assertFalse((archive / rotation.ACCOUNTS).exists())
        self.assertEqual((archive / rotation.LEDGER).read_bytes(), self.controls[rotation.LEDGER])
        self.assertFalse(self.ledger.exists())

    def test_invalid_binding_and_foreign_receipt_refuse_without_effect(self):
        self.binding.write_text('{"accounts":"invalid"}')
        with self.assertRaises(rotation.RotationError):
            self.rotate()
        self.assertFalse(rotation.pending(self.home))
        self.assertEqual(fingerprint(self.profile), self.before)
        self.binding.write_bytes(self.controls[rotation.ACCOUNTS])
        with self.assertRaises(rotation.RotationError):
            rotation.rotate(self.home, self.root, self.state, receipts)
        self.assert_original()

    def test_symlink_and_hardlinked_controls_refuse_without_following(self):
        self.binding.unlink()
        external = self.root / 'foreign'
        external.write_text('foreign')
        symlink_or_skip(external, self.binding)
        with self.assertRaises((rotation.RotationError, OSError, ValueError)):
            self.rotate()
        self.assertEqual(external.read_text(), 'foreign')
        self.binding.unlink()
        try:
            os.link(external, self.binding)
        except OSError:
            self.skipTest('hard links unavailable')
        with self.assertRaises(rotation.RotationError):
            self.rotate()
        self.assertEqual(external.read_text(), 'foreign')
        self.assertFalse(rotation.pending(self.home))

    def test_profile_symlink_refuses_without_touching_target(self):
        external = self.root / 'outside'
        external.write_text('untouched')
        symlink_or_skip(external, self.profile / 'linked')
        with self.assertRaises((rotation.RotationError, OSError, ValueError)):
            self.rotate()
        self.assertEqual(external.read_text(), 'untouched')
        self.assertFalse(rotation.pending(self.home))

    def test_state_or_receipt_changed_after_preflight_never_displaces_profile(self):
        for name in (rotation.STATE, 'activation.json'):
            with self.subTest(name=name):
                path = self.home / name
                old = path.read_bytes()
                stage = rotation._stage
                def mutate(*args):
                    result = stage(*args)
                    path.write_bytes(old + b'\n')
                    return result
                with mock.patch.object(rotation, '_stage', side_effect=mutate):
                    with self.assertRaises(rotation.RotationError):
                        self.rotate()
                self.assertTrue(rotation.pending(self.home))
                self.assertEqual(fingerprint(self.profile), self.before)
                self.assertEqual(path.read_bytes(), old + b'\n')
                # Recovery before the displacement never rewrites the edit.
                self.assertEqual(rotation.recover(self.home, receipts), 'restored')
                self.assertEqual(path.read_bytes(), old + b'\n')
                path.write_bytes(old)

    def test_existing_archive_collision_is_never_overwritten(self):
        fixed = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        archive, _ = rotation._paths(self.profile, fixed)
        archive.mkdir()
        sentinel = archive / 'untouched'
        sentinel.write_text('retained')
        with mock.patch.object(rotation.uuid, 'uuid4', return_value=fixed):
            with self.assertRaises(rotation.RotationError):
                self.rotate()
        self.assertEqual(sentinel.read_text(), 'retained')
        self.assert_original()

    def test_abrupt_interruptions_recover_original_after_each_effect(self):
        boundaries = ['move-%s-%s' % (side, index)
                      for index in range(1, 4) for side in ('before', 'after')]
        boundaries += ['receipt-before', 'receipt-after']
        boundaries += ['control-%d-%s' % (index, side) for index in (1, 2) for side in ('before', 'after')]
        boundaries += ['finish-before', 'finish-after']
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                self.setUp()
                run = subprocess.run([sys.executable, '-c', CRASH, str(rotation.__file__),
                                      str(self.home), str(self.profile), boundary],
                                     capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode, 73, run.stderr)
                if boundary == 'finish-after':
                    # The marker was removed only after the committed readback.
                    self.assertFalse(rotation.pending(self.home))
                    self.assertEqual(rotation.recover(self.home, receipts), 'none')
                    archive, = self.archives()
                    self.assertEqual(fingerprint(archive / 'profile'), self.before)
                elif boundary == 'move-before-1':
                    self.assertFalse(rotation.pending(self.home))
                    self.assert_original()
                else:
                    self.assertTrue(rotation.pending(self.home))
                    old_root = rotation.recovery_root(self.home, receipts)
                    expected = (self.profile if boundary in ('move-after-1', 'move-before-2')
                                else self.archives()[0] / 'profile')
                    self.assertEqual(old_root, expected)
                    self.assertFalse((self.profile / '.claude-profile-sync.lock').exists())
                    self.assertEqual(rotation.recover(self.home, receipts), 'restored')
                    self.assert_original()

    def test_interrupted_recovery_is_retryable_at_each_rename(self):
        for index in (1, 2, 'finish'):
            for side in ('before', 'after'):
                with self.subTest(index=index, side=side):
                    self.setUp()
                    crash = subprocess.run([sys.executable, '-c', CRASH, str(rotation.__file__),
                        str(self.home), str(self.profile), 'receipt-after'],
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(crash.returncode, 73, crash.stderr)
                    from tests.test_claude_account_recovery import RECOVERY_CRASH
                    stop = subprocess.run([sys.executable, '-c', RECOVERY_CRASH,
                        str(rotation.__file__), str(self.home),
                        'finish-' + side if index == 'finish' else side + '-' + str(index)],
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(stop.returncode, 73, stop.stderr)
                    if side == 'after' and index == 'finish':
                        self.assertEqual(rotation.recover(self.home, receipts), 'none')
                    else:
                        self.assertTrue(rotation.pending(self.home))
                        old_root = rotation.recovery_root(self.home, receipts)
                        expected = self.archives()[0] / 'profile'
                        self.assertEqual(old_root, expected if expected.exists() else self.profile)
                        self.assertEqual(rotation.recover(self.home, receipts), 'restored')
                    self.assert_original()

    def test_concurrent_receipt_edit_during_pending_recovery_is_never_overwritten(self):
        mover = rotation._publish.move_new
        def interrupt(source, target):
            mover(source, target)
            if Path(source) == self.profile:
                raise OSError('synthetic interruption')
        with mock.patch.object(rotation._publish, 'move_new', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.rotate()
        changed = self.receipt_before + b'\n'
        self.receipt_path.write_bytes(changed)
        with self.assertRaises(rotation.RotationError):
            rotation.recover(self.home, receipts)
        self.assertTrue(rotation.pending(self.home))
        self.assertEqual(self.receipt_path.read_bytes(), changed)
        self.assertEqual(fingerprint(self.archives()[0] / 'profile'), self.before)


if __name__ == '__main__':
    unittest.main()
