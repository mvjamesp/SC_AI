"""Restart failures remain actionable, pending and quiet until a new reason arrives."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_native_setup import module, resources


class WindowsNoticeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.path = self.home / 'update-notice.json'
        self.doc = dict(version='1.0.0', clients='codex', attachment='synthetic-event',
                        names=['Example: Model 1'])
        self.path.write_text(json.dumps(self.doc))
        self.owner = module('notice_windows')
        self.dialog = mock.Mock()
        self.dialog.show.return_value = True
        self.platform = mock.Mock()
        self.native = mock.MagicMock()
        self.platform.request_stop.return_value = True
        self.text = self.owner.load('notice_text')
        self.enterContext(mock.patch.object(self.text, 'acknowledge'))
        self.log = mock.Mock()
        service = mock.Mock(install_lock=lambda *args: contextlib.nullcontext(True))
        real = self.owner.load
        owners = dict(windows_dialog=self.dialog, claude_desktop_platform=self.platform,
                      native_dialog=self.native, notice_text=self.text, service=service, update_reconcile=self.log)
        self.enterContext(mock.patch.object(self.owner, 'load',
                                            side_effect=lambda name: owners.get(name) or real(name)))

    def read(self):
        return json.loads(self.path.read_text())

    def test_only_verified_quit_and_visible_reopen_complete_the_restart(self):
        def reopen(*args, **kwargs):
            self.native.Progress.return_value.__enter__.assert_called_once()
            self.native.Progress.return_value.__exit__.assert_not_called()
        self.platform.open_app.side_effect = reopen
        self.owner.show(self.home)
        self.native.Progress.return_value.__exit__.assert_called_once()
        self.assertEqual(self.native.Progress.call_args.kwargs['button'], 'OK')
        self.assertIn('restart will continue', self.native.Progress.call_args.args[0])
        self.assertTrue(self.read()['shown'])
        self.assertFalse(self.read()['restart_pending'])
        self.assertEqual(self.read()['restart_result'], 'done')
        identity = self.platform.app_identity.return_value
        app = self.platform.App.return_value
        self.platform.request_stop.assert_called_once_with('win32', app=app, identity=identity)
        self.platform.open_app.assert_called_once_with('win32', app=app, identity=identity)
        self.assertEqual(self.dialog.show.call_args.kwargs['labels'], ('Restart Codex', 'Later'))
        self.assertIn('Finish active work', self.dialog.show.call_args.args[0])
        self.text.acknowledge.assert_called_once_with(self.home, self.doc['names'])

    def test_discovery_quit_and_reopen_failures_are_pending_without_repeated_prompts(self):
        for stage in ('app_identity', 'request_stop', 'open_app'):
            with self.subTest(stage):
                self.path.write_text(json.dumps(self.doc))
                self.platform.reset_mock()
                self.dialog.reset_mock()
                target = getattr(self.platform, stage)
                target.side_effect = OSError('synthetic-private-value')
                try:
                    self.owner.show(self.home)
                finally:
                    target.side_effect = None
                self.assertTrue(self.read()['shown'])
                self.assertTrue(self.read()['restart_pending'])
                self.assertEqual(self.read()['restart_result'], 'failed')
                if stage != 'open_app':
                    self.platform.open_app.assert_not_called()
                self.assertEqual(self.dialog.show.call_count, 2)
                text = self.dialog.show.call_args.args[0]
                self.assertIn('quit and reopen Codex yourself', text)
                self.assertNotIn('synthetic-private-value', text)
                self.assertEqual(self.dialog.show.call_args.kwargs['labels'], ('OK',))
                self.assertFalse(self.owner.offer(self.home, spawn=mock.Mock()))
                self.owner.show(self.home)
                self.assertEqual(self.dialog.show.call_count, 2)

    def test_a_false_shutdown_result_does_not_reopen_or_claim_success(self):
        self.platform.request_stop.return_value = False
        self.owner.show(self.home)
        self.platform.open_app.assert_not_called()
        self.assertEqual(self.read()['restart_result'], 'failed')
        self.assertTrue(self.read()['restart_pending'])

    def test_later_never_resolves_identity_or_closes_any_app(self):
        self.dialog.show.return_value = False
        self.owner.show(self.home)
        self.platform.app_identity.assert_not_called()
        self.platform.request_stop.assert_not_called()
        self.assertTrue(self.read()['restart_pending'])
        self.assertEqual(self.read()['restart_result'], 'later')
        self.assertFalse(self.owner.offer(self.home, spawn=mock.Mock()))

    def test_setup_completion_shows_unknown_freshness_once_and_reuses_attachment_event(self):
        self.dialog.show.return_value = False
        self.assertTrue(self.owner.show(self.home, setup_completion=True))
        shown = self.read()
        self.assertTrue(shown['shown'])
        self.assertTrue(shown['restart_pending'])
        self.assertEqual(shown['restart_result'], 'later')
        self.assertEqual(shown['freshness'], 'unknown')
        self.assertIn("We couldn't confirm Codex loaded the changes", self.dialog.show.call_args.args[0])
        self.assertIn('Finish any running work first. Restarting will close and reopen Codex.',
                      self.dialog.show.call_args.args[0])
        self.assertIn('choose Check setup', self.dialog.show.call_args.args[0])
        self.assertEqual(self.dialog.show.call_args.kwargs['labels'], ('Restart Codex', 'Later'))
        self.platform.request_stop.assert_not_called()
        self.assertTrue(self.owner.show(self.home, setup_completion=True))
        self.assertEqual(self.dialog.show.call_count, 1)

    def test_setup_completion_does_not_offer_or_launch_a_closed_codex(self):
        self.platform.is_running.return_value = False
        self.assertFalse(self.owner.show(self.home, setup_completion=True))
        self.dialog.show.assert_not_called()
        self.platform.request_stop.assert_not_called()
        self.assertNotIn('shown', self.read())

    def test_setup_completion_restart_uses_current_verified_desktop_and_records_unknown(self):
        identity = {'package': 'current-user-codex'}
        self.platform.app_identity.return_value = identity
        self.assertTrue(self.owner.show(self.home, setup_completion=True))
        self.assertEqual(self.read()['freshness'], 'unknown')
        self.assertEqual(self.read()['restart_result'], 'done')
        self.platform.is_running.assert_called_with('win32', app=self.platform.App.return_value)
        self.platform.request_stop.assert_called_once_with(
            'win32', app=self.platform.App.return_value, identity=identity)

    def test_catalogue_only_change_is_a_pending_setup_restart_reason(self):
        self.path.write_text(json.dumps(dict(version='1.0.1', clients='codex',
                                              client_change='synthetic-change')))
        self.assertTrue(self.owner.show(self.home, setup_completion=True))
        self.assertEqual(self.read()['freshness'], 'unknown')
        self.assertIn("We couldn't confirm Codex loaded the changes", self.dialog.show.call_args.args[0])

    def test_unconfirmed_or_unavailable_dialog_leaves_notice_unconsumed(self):
        self.dialog.show.side_effect = OSError('no presentation')
        with self.assertRaises(OSError):
            self.owner.show(self.home)
        self.assertNotIn('shown', self.read())
        self.platform.app_identity.assert_not_called()

    def test_a_new_notice_during_reopen_is_not_overwritten_by_the_old_result(self):
        newer = dict(self.doc, version='1.0.1', names=['Example: Model 2'])
        self.platform.open_app.side_effect = lambda *a, **k: self.path.write_text(json.dumps(newer))
        with self.assertRaises(OSError):
            self.owner.show(self.home)
        self.assertEqual(self.read(), newer)

    def test_a_new_notice_while_asking_is_preserved_with_actionable_feedback(self):
        newer = dict(self.doc, version='1.0.1')
        def answer(*args, **kwargs):
            self.path.write_text(json.dumps(newer))
            return True
        self.dialog.show.side_effect = lambda *a, **k: answer() if self.dialog.show.call_count == 1 else True
        self.owner.show(self.home)
        self.assertEqual(self.read(), newer)
        self.platform.app_identity.assert_not_called()
        self.assertIn('quit and reopen Codex yourself', self.dialog.show.call_args.args[0])

    def test_claude_only_is_informational_and_never_restarts_codex(self):
        self.path.write_text(json.dumps(dict(self.doc, clients='claude', attachment='')))
        self.owner.show(self.home)
        self.assertIn('Restart Claude Code', self.dialog.show.call_args.args[0])
        self.assertEqual(self.dialog.show.call_args.kwargs['labels'], ('OK',))
        self.platform.app_identity.assert_not_called()
        self.assertFalse(self.read()['restart_pending'])

    def test_a_held_notice_lock_does_not_stack_another_dialog(self):
        real = self.owner.load
        service = mock.Mock(install_lock=lambda *args: contextlib.nullcontext(False))
        with mock.patch.object(self.owner, 'load',
                side_effect=lambda name: service if name == 'service' else real(name)):
            self.owner.show(self.home)
        self.dialog.show.assert_not_called()

    def test_setup_completion_does_not_stack_on_an_active_notice(self):
        real = self.owner.load
        service = mock.Mock(install_lock=lambda *args: contextlib.nullcontext(False))
        with mock.patch.object(self.owner, 'load',
                side_effect=lambda name: service if name == 'service' else real(name)):
            self.assertTrue(self.owner.show(self.home, setup_completion=True))
        self.dialog.show.assert_not_called()

    def test_launch_uses_retained_python_and_source_and_does_not_consume_the_notice(self):
        retained = resources(self.home / 'setup/fixture')
        worker = retained / 'router/packaging/notice_windows.py'
        worker.write_text('# synthetic worker')
        (self.home / 'setup-current').write_text('fixture')
        real = self.owner.load
        payload = mock.Mock(return_value=dict(runtime_exec='python/bin/python3.14'))
        with mock.patch.object(self.owner, 'metadata', payload):
            spawn = mock.Mock()
            self.assertTrue(self.owner.offer(self.home, spawn))
        self.assertEqual(spawn.call_args.args[0], [str(retained / 'runtime/python/bin/python3.14'),
                                                 str(worker), '--home', str(self.home)])
        self.assertNotIn('shown', self.read())

    def test_absent_retained_resources_or_failed_spawn_keep_the_notice_pending(self):
        spawn = mock.Mock()
        self.assertFalse(self.owner.offer(self.home, spawn))
        spawn.assert_not_called()
        with mock.patch.object(self.owner, 'command', return_value=['worker']):
            self.assertFalse(self.owner.offer(self.home, mock.Mock(side_effect=OSError('blocked'))))
        self.assertNotIn('shown', self.read())


if __name__ == '__main__':
    unittest.main()
