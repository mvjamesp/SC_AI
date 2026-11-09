"""Clean account profiles must still support removal and later attachment."""
import json
import unittest
import contextlib
import io
import sys
from types import SimpleNamespace
from unittest import mock

from tests.claude_profile_sync_fixtures import load_helper
from tests import test_claude_profile_rotation as cases

desktop_config, receipts = cases.desktop_config, cases.receipts


class RotationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.rig = cases.RotationTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        self.removal = load_helper('client_removal')

    def interrupt_rotation(self):
        move = cases.rotation._publish.move_new
        def interrupted(source, target):
            move(source, target)
            if source == self.rig.profile:
                raise OSError('synthetic interruption after profile retention')
        with mock.patch.object(cases.rotation._publish, 'move_new', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.rig.rotate()

    def test_scheduled_update_status_does_not_prevent_pending_recovery(self):
        self.interrupt_rotation()
        cases.install_mod._updater._record_pass(self.rig.home, error='',
            latest={'version': '1.0.0'}, feed='https://example.invalid/latest.json',
            etag='synthetic-etag', channel='stable')
        state = self.rig.home / cases.rotation.STATE
        updated = state.read_bytes()
        self.assertIn('last_check_at', json.loads(updated))
        self.assertEqual(cases.rotation.recover(self.rig.home, receipts), 'restored')
        self.rig.assert_original()
        self.assertEqual(state.read_bytes(), updated)
        archive, = self.rig.archives()
        self.assertEqual(json.loads((archive / cases.rotation.STATE).read_bytes()), self.rig.state)

    def test_recovery_refuses_configuration_changes_but_keeps_the_original_profile(self):
        self.interrupt_rotation()
        path = self.rig.home / cases.rotation.STATE
        for key, value in (('port', 2), ('port', True), ('token', 'different-synthetic-token'),
                           ('claude_mode', 'none'), ('openai_models', ['different-model']),
                           ('unknown-setting', True)):
            with self.subTest(key=key):
                changed = dict(self.rig.state, **{key: value})
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(cases.rotation.RotationError, 'changed-state'):
                    cases.rotation.recover(self.rig.home, receipts)
                self.assertTrue(cases.rotation.pending(self.rig.home))
                self.assertEqual(json.loads(path.read_bytes()), changed)
        path.write_text(json.dumps(self.rig.state))
        self.assertEqual(cases.rotation.recover(self.rig.home, receipts), 'restored')
        self.rig.assert_original()

    def test_rollback_refuses_pending_rotation_before_selecting_an_older_release(self):
        replace = receipts.replace_receipt
        def interrupted(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError('synthetic interruption after receipt publication')
        with mock.patch.object(receipts, 'replace_receipt', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.rig.rotate()
        args = cases.install_mod.build_parser().parse_args(
            ['--home', str(self.rig.home), '--version', '1.0.0'])
        bundle = cases.install_mod._bundle
        backend, swap = mock.Mock(), mock.Mock()
        with self.assertRaisesRegex(SystemExit, 'recover.*Claude profile'), \
                mock.patch.object(cases.install_mod, 'platform_backend', backend), \
                mock.patch.object(cases.install_mod, 'swap_current', swap):
            cases.install_mod.do_rollback(args)
        backend.assert_not_called()
        swap.assert_not_called()
        self.assertTrue(cases.rotation.pending(self.rig.home))

    def test_repair_refuses_before_terminal_receipt_refresh_then_recovery_still_works(self):
        receipts.write_client_receipt(self.rig.home, 'claude', {'keep': 'terminal receipt'})
        move = cases.rotation._publish.move_new
        def interrupted(source, target):
            move(source, target)
            if source == self.rig.profile:
                raise OSError('synthetic interruption after original retention')
        with mock.patch.object(cases.rotation._publish, 'move_new', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.rig.rotate()
        before = self.rig.receipt_path.read_bytes()
        args = cases.install_mod.build_parser().parse_args(
            ['--home', str(self.rig.home), '--repair', '--clients', 'claude', '--desktop'])
        choice, terminal, ready = cases.install_mod._client_choice, mock.Mock(), mock.Mock()
        terminal.side_effect = lambda *a, **k: receipts.write_client_receipt(
            self.rig.home, 'claude', {'refreshed': 'terminal receipt'})
        with self.assertRaisesRegex(SystemExit, 'recover.*Claude profile'):
            choice.attach_current(args, self.rig.home, codex=receipts,
                desktop=desktop_config, claude=terminal, python='synthetic-python',
                write_state=mock.Mock(), resolve=lambda *a: ('codex', 'claude', 'subscription', 'subscription'),
                ensure_state=lambda *a: self.rig.state.copy(), ready=ready)
        terminal.assert_not_called()
        ready.assert_not_called()
        self.assertEqual(self.rig.receipt_path.read_bytes(), before)
        self.assertEqual(cases.rotation.recover(self.rig.home, receipts), 'restored')

    def test_install_repair_is_refused_before_delivery_or_state_changes(self):
        (self.rig.home / cases.rotation.PENDING).write_text('{"pending":true}')
        args = cases.install_mod.build_parser().parse_args(['--home', str(self.rig.home), '--repair'])
        delivery = mock.Mock()
        with mock.patch.object(cases.install_mod._delivery, 'run_install', delivery), \
                self.assertRaisesRegex(SystemExit, 'recover.*Claude profile'):
            cases.install_mod.do_install(args)
        delivery.assert_not_called()

    def test_pending_rotation_freezes_other_client_receipts_and_automatic_repair(self):
        self.interrupt_rotation()
        before = self.rig.receipt_path.read_bytes()
        with self.assertRaisesRegex(SystemExit, 'recover.*Claude profile'):
            receipts.write_client_receipt(self.rig.home, 'codex', {'changed': 'blocked'})
        with self.assertRaisesRegex(SystemExit, 'recover.*Claude profile'):
            receipts.drop_client(self.rig.home, 'codex')
        recovery, service = load_helper('codex_recovery'), mock.Mock()
        with mock.patch.object(recovery, 'load') as load:
            self.assertEqual(recovery.reconcile(self.rig.home, service), 'deferred')
        load.assert_not_called()
        self.assertEqual(self.rig.receipt_path.read_bytes(), before)
        self.assertEqual(cases.rotation.recover(self.rig.home, receipts), 'restored')

    @unittest.skipIf(sys.version_info < (3, 11), 'existing updater fixture requires tomllib')
    def test_scheduled_pending_hold_never_parks_a_release_and_recovery_allows_it(self):
        from tests import test_update_channel as update_cases
        self.interrupt_rotation()
        updater, installer = cases.install_mod._updater, cases.install_mod
        rig = update_cases.UpdateBackoffTests()
        self.addCleanup(rig.doCleanups)
        blob, latest = update_cases._craft_bundle('9.9.9')
        def child(command, **kwargs):
            if command[:1] == ['launchctl']:
                return update_cases._ok_run(command, **kwargs)
            args = installer.build_parser().parse_args(['--home', str(self.rig.home)])
            output = io.StringIO()
            try:
                with mock.patch.dict('os.environ', kwargs['env']), \
                        mock.patch.object(installer._delivery, 'run_install', return_value=0), \
                        contextlib.redirect_stdout(output):
                    result = installer.do_install(args)
            except SystemExit as error:
                return SimpleNamespace(returncode=1, stdout=output.getvalue(), stderr=str(error))
            if not cases.rotation.pending(self.rig.home):
                (self.rig.home / 'releases/9.9.9').mkdir(parents=True, exist_ok=True)
                (self.rig.home / 'current').write_text('9.9.9')
            return SimpleNamespace(returncode=result, stdout=output.getvalue(), stderr='')
        for _ in range(installer.MAX_VERSION_FAILURES + 1):
            result, output = rig._check(self.rig.home, blob, latest, [], run=child)
            self.assertEqual(result, 0, output)
        state = json.loads((self.rig.home / cases.rotation.STATE).read_bytes())
        self.assertNotIn('last_error', state)
        self.assertNotIn('installation_repair_required', state)
        self.assertEqual(updater.update_failures(self.rig.home), {})
        self.assertEqual(cases.rotation.recover(self.rig.home, receipts), 'restored')
        result, output = rig._check(self.rig.home, blob, latest, [], run=child)
        self.assertEqual(result, 0, output)
        self.assertEqual(installer.current_version(self.rig.home), '9.9.9')

    def test_rotated_subscription_profile_can_be_removed_and_reattached(self):
        self.rig.rotate()
        self.removal.detach_json(self.rig.home, 'claude-desktop', receipts)
        config = self.rig.profile / 'claude_desktop_config.json'
        self.assertNotIn('deploymentMode', json.loads(config.read_bytes()))
        path = desktop_config.attach(self.rig.home, self.rig.state, receipts,
                                      data_dir_override=self.rig.profile)
        self.assertEqual(json.loads(path.read_bytes()),
                         desktop_config.desired_values(self.rig.state))
        archive, = self.rig.archives()
        self.assertTrue((archive / 'profile').is_dir())
        self.assertEqual((archive / 'activation.json').read_bytes(), self.rig.receipt_before)

    def test_user_mode_selection_after_rotation_is_preserved_on_removal(self):
        self.rig.rotate()
        config = self.rig.profile / 'claude_desktop_config.json'
        selected = b'{"deploymentMode":"1p","personSetting":true}\n'
        config.write_bytes(selected)
        self.removal.detach_json(self.rig.home, 'claude-desktop', receipts)
        self.assertEqual(config.read_bytes(), selected)
        self.assertNotIn('claude-desktop', receipts.read_receipt(self.rig.home).get('clients', {}))


if __name__ == '__main__':
    unittest.main()
