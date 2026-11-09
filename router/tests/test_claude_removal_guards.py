"""Incomplete removal must remain visible and safely retryable."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
import subprocess
from unittest import mock
from tests import test_removal_recovery as removal_cases
from tests import test_claude_sealed_gateway as sealed_cases
from tests.claude_profile_sync_fixtures import load_helper


class RemovalPointerGuardsTests(unittest.TestCase):
    def setUp(self):
        self.rig = removal_cases.RemovalRecoveryTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)

    def test_unlink_failure_reports_incomplete_removal_and_retry_finishes(self):
        rig = self.rig
        unlink, output = Path.unlink, io.StringIO()

        def fail_pointer(path, *args, **kwargs):
            if path == rig.home / 'current':
                raise PermissionError('synthetic sharing violation')
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', new=fail_pointer), \
                contextlib.redirect_stdout(output), self.assertRaisesRegex(SystemExit, 'retry removal'):
            rig.install.do_uninstall(rig.args)
        self.assertNotIn('uninstalled services', output.getvalue())
        self.assertTrue((rig.home / 'current').exists())
        self.assertEqual(rig.remove(), 0)
        self.assertFalse((rig.home / 'current').exists())
        self.assertEqual(rig.install.current_version(rig.home), '0.0.0')
        self.assertTrue((rig.home / 'releases/1.2.6.174').is_dir())

    def test_service_failure_keeps_the_current_pointer(self):
        rig = self.rig
        rig.backend.uninstall.side_effect = OSError('synthetic service failure')
        with self.assertRaises(OSError):
            rig.remove()
        self.assertTrue((rig.home / 'current').exists())

    def test_linux_retry_does_not_disable_already_removed_units(self):
        systemd = load_helper('systemd')
        def run(command, **kwargs):
            if 'disable' in command and kwargs.get('check'):
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, '', '')
        manager = systemd.Manager(self.rig.home, run)
        manager.units = self.rig.home / 'removed-units'
        manager.uninstall()


class DesktopRemovalGuardsTests(unittest.TestCase):
    def setUp(self):
        self.rig = sealed_cases.SealedGatewayTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)

    def test_utf8_metadata_proof_does_not_depend_on_the_windows_locale(self):
        rig = self.rig
        meta = rig.profile / 'configLibrary' / '_meta.json'
        value = json.loads(meta.read_bytes())
        value['name'] = 'Équipe synthétique'
        meta.write_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        open_file = Path.open
        def locale_open(path, mode='r', *args, **kwargs):
            if path == meta and 'b' not in mode and kwargs.get('encoding') is None:
                kwargs['encoding'] = 'cp1252'
            return open_file(path, mode, *args, **kwargs)
        with mock.patch.object(Path, 'open', locale_open):
            removal = rig.rig.install._delivery._load('client_removal')
            entry = rig.rig.owner.read_receipt(rig.rig.home)['clients']['claude-desktop']
            removal.detach_json(rig.rig.home, 'claude-desktop', rig.rig.owner, entry)
        self.assertEqual(json.loads(meta.read_bytes()), value)

    def test_unsealed_utf8_selection_metadata_uses_byte_guard_in_a_legacy_locale(self):
        rig = self.rig
        doc = json.loads(rig.entry.read_bytes())
        doc[rig.owner.API_KEY_KEY] = rig.owner.API_KEY_VALUE
        rig.entry.write_text(json.dumps(doc))
        meta = rig.profile / 'configLibrary' / '_meta.json'
        value = json.loads(meta.read_bytes())
        value['name'] = '丁'
        meta.write_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        open_file = Path.open
        def locale_open(path, mode='r', *args, **kwargs):
            if path == meta and 'b' not in mode and kwargs.get('encoding') is None:
                kwargs['encoding'] = 'cp1252'
            return open_file(path, mode, *args, **kwargs)
        with mock.patch.object(Path, 'open', locale_open):
            removal = rig.rig.install._delivery._load('client_removal')
            entry = rig.rig.owner.read_receipt(rig.rig.home)['clients']['claude-desktop']
            removal.detach_json(rig.rig.home, 'claude-desktop', rig.rig.owner, entry)
        self.assertEqual(json.loads(meta.read_bytes()), value)

    def test_interrupted_archive_write_can_retry_without_retiring_opaque_settings(self):
        rig = self.rig
        removal = rig.rig.install._delivery._load('client_removal')
        entry = rig.rig.owner.read_receipt(rig.rig.home)['clients']['claude-desktop']
        plan = removal.json_plan(rig.rig.home, 'claude-desktop', entry, rig.rig.owner)
        before = rig.entry.read_bytes()
        fdopen = removal.os.fdopen
        def interrupted(fd, mode, *args, **kwargs):
            stream = fdopen(fd, mode, *args, **kwargs)
            class Partial:
                def __enter__(self): return self
                def __exit__(self, *_args): stream.close()
                def write(self, raw):
                    stream.write(raw[:len(raw) // 2])
                    raise OSError('synthetic disk-full archive write')
            return Partial()
        with mock.patch.object(removal.os, 'fdopen', side_effect=interrupted):
            with self.assertRaises(OSError):
                removal.apply(plan)
        self.assertEqual(rig.entry.read_bytes(), before)
        self.assertFalse(list(rig.rig.home.glob('claude-desktop-before-*.json')))
        removal.apply(plan)
        saved, = rig.rig.home.glob('claude-desktop-before-*.json')
        self.assertEqual(saved.read_bytes(), before)

    def test_changed_seal_is_retained_exactly_before_removal_and_reattach(self):
        rig = self.rig
        rig.config.write_text('{"keep":true}\n')
        rig.seal(sealed_cases.sealed(b'a person replaced this opaque value'))
        original = rig.entry.read_bytes()
        self.assertEqual(rig.rig.remove(), 0)
        saved = list(rig.rig.home.glob('claude-desktop-before-*.json'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), original)
        if os.name != 'nt':
            self.assertEqual(saved[0].stat().st_mode & 0o077, 0)
        self.assertNotIn(rig.owner.API_KEY_KEY, json.loads(rig.entry.read_text()))
        self.assertEqual(rig.owner.attach(rig.rig.home, rig.rig.state, rig.rig.owner,
                                         data_dir_override=rig.profile), rig.entry)
        self.assertEqual(saved[0].read_bytes(), original)

    def test_preservation_failure_keeps_desktop_settings_receipt_and_service(self):
        rig = self.rig
        removal = rig.rig.install._delivery._load('client_removal')
        load, open_file = rig.rig.install._delivery._load, removal.os.open
        before = rig.entry.read_bytes(), rig.config.read_bytes()

        def fail_archive(path, *args, **kwargs):
            if Path(path).name.startswith('claude-desktop-before-'):
                raise OSError('synthetic archive failure')
            return open_file(path, *args, **kwargs)

        with mock.patch.object(rig.rig.install._delivery, '_load', side_effect=lambda name:
                removal if name == 'client_removal' else load(name)), \
                mock.patch.object(removal.os, 'open', side_effect=fail_archive), \
                self.assertRaises(SystemExit):
            rig.rig.remove()
        self.assertEqual((rig.entry.read_bytes(), rig.config.read_bytes()), before)
        self.assertIn('claude-desktop', rig.rig.owner.read_receipt(rig.rig.home)['clients'])
        rig.rig.backend.uninstall.assert_not_called()

    def test_selection_changed_after_preflight_keeps_receipt_and_service(self):
        rig = self.rig
        removal = rig.rig.install._delivery._load('client_removal')
        load, preflight = rig.rig.install._delivery._load, removal.preflight
        meta = rig.profile / 'configLibrary/_meta.json'
        changed_id = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        original = rig.entry.read_bytes()

        def change_selection(*args):
            plans = preflight(*args)
            (meta.parent / (changed_id + '.json')).write_bytes(original)
            doc = json.loads(meta.read_text())
            doc['appliedId'] = changed_id
            meta.write_text(json.dumps(doc))
            return plans

        with mock.patch.object(rig.rig.install._delivery, '_load', side_effect=lambda name:
                removal if name == 'client_removal' else load(name)), \
                mock.patch.object(removal, 'preflight', side_effect=change_selection), \
                self.assertRaisesRegex(SystemExit, 'left running'):
            rig.rig.remove()
        self.assertEqual(rig.entry.read_bytes(), original)
        self.assertIn('claude-desktop', rig.rig.owner.read_receipt(rig.rig.home)['clients'])
        rig.rig.backend.uninstall.assert_not_called()
        self.assertEqual(json.loads(meta.read_text())['appliedId'], changed_id)

    def test_proof_state_change_after_preflight_is_not_used_for_a_write(self):
        rig = self.rig
        removal = rig.rig.install._delivery._load('client_removal')
        entry = rig.rig.owner.read_receipt(rig.rig.home)['clients']['claude-desktop']
        plan = removal.json_plan(rig.rig.home, 'claude-desktop', entry, rig.rig.owner)
        path = rig.rig.home / 'mixed-router-state.json'
        path.write_text(json.dumps(dict(rig.rig.state, port=4321)))
        before = rig.protected()
        with self.assertRaises(OSError):
            removal.apply(plan)
        self.assertEqual(rig.protected(), before)

    def test_missing_mode_file_created_after_preflight_refuses_without_writes(self):
        rig = self.rig
        rig.config.unlink()
        removal = rig.rig.install._delivery._load('client_removal')
        entry = rig.rig.owner.read_receipt(rig.rig.home)['clients']['claude-desktop']
        plan = removal.json_plan(rig.rig.home, 'claude-desktop', entry, rig.rig.owner)
        rig.config.write_text('{"deploymentMode":"3p","person_setting":true}')
        before = rig.protected()
        with self.assertRaises(OSError):
            removal.apply(plan)
        self.assertEqual(rig.protected(), before)

    def test_metadata_change_after_entry_restore_keeps_retry_refused(self):
        rig = self.rig
        removal = rig.rig.install._delivery._load('client_removal')
        load, replace = rig.rig.install._delivery._load, removal._atomic.replace_text
        meta = rig.profile / 'configLibrary/_meta.json'
        original = meta.read_bytes()

        def change_metadata(path, *args, **kwargs):
            answer = replace(path, *args, **kwargs)
            if path == rig.entry.resolve():
                doc = json.loads(meta.read_text())
                doc['appliedId'] = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
                meta.write_text(json.dumps(doc))
            return answer

        with mock.patch.object(rig.rig.install._delivery, '_load', side_effect=lambda name:
                removal if name == 'client_removal' else load(name)), \
                mock.patch.object(removal._atomic, 'replace_text', side_effect=change_metadata), \
                self.assertRaisesRegex(SystemExit, 'left running'):
            rig.rig.remove()
        with self.assertRaisesRegex(SystemExit, 'No assistants were detached'):
            rig.rig.remove()
        rig.rig.backend.uninstall.assert_not_called()
        self.assertIn('claude-desktop', rig.rig.owner.read_receipt(rig.rig.home)['clients'])
        meta.write_bytes(original)
        self.assertEqual(rig.rig.remove(), 0)

    def test_new_archive_removed_before_restore_keeps_opaque_entry(self):
        rig = self.rig
        removal = rig.rig.install._delivery._load('client_removal')
        entry = rig.rig.owner.read_receipt(rig.rig.home)['clients']['claude-desktop']
        plan = removal.json_plan(rig.rig.home, 'claude-desktop', entry, rig.rig.owner)
        archive = next(path for path, _, after in plan if isinstance(after, bytes))
        check = removal._atomic.check_unchanged
        original = rig.entry.read_bytes()

        def lose_archive(path, expected):
            if path.name == '_meta.json' and archive.exists():
                archive.unlink()
            return check(path, expected)

        with mock.patch.object(removal._atomic, 'check_unchanged', side_effect=lose_archive), \
                self.assertRaises(OSError):
            removal.apply(plan)
        self.assertEqual(rig.entry.read_bytes(), original)
        self.assertIn('claude-desktop', rig.rig.owner.read_receipt(rig.rig.home)['clients'])

    def test_competing_archive_is_not_replaced_after_absence_check(self):
        rig = self.rig
        removal = rig.rig.install._delivery._load('client_removal')
        entry = rig.rig.owner.read_receipt(rig.rig.home)['clients']['claude-desktop']
        plan = removal.json_plan(rig.rig.home, 'claude-desktop', entry, rig.rig.owner)
        archive = next(path for path, _, after in plan if isinstance(after, bytes))
        atomic = load_helper('claude_profile_rotation')
        move = atomic._publish.move_new
        load = removal._load
        original = rig.entry.read_bytes()

        def compete(source, target):
            if target == archive:
                archive.write_bytes(b'competing retained entry')
            return move(source, target)

        with mock.patch.object(removal, '_load', side_effect=lambda name:
                atomic if name == 'claude_profile_rotation' else load(name)), \
                mock.patch.object(atomic._publish, 'move_new', side_effect=compete), \
                self.assertRaises(OSError):
            removal.apply(plan)
        self.assertEqual(archive.read_bytes(), b'competing retained entry')
        self.assertEqual(rig.entry.read_bytes(), original)
