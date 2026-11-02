"""The sign-in wait remains visible and cancellation never submits a late key."""
import contextlib
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ai_router import key_prompt, work_account
from tests.test_native_setup import module


class WaitingTests(unittest.TestCase):
    def setUp(self):
        self.native = mock.MagicMock()
        self.context = self.native.Progress.return_value
        self.progress = self.context.__enter__.return_value
        self.progress.process.poll.return_value = None
        self.progress.cancelled = False
        self.enterContext(mock.patch.object(work_account, '_load', return_value=self.native))

    def test_wait_covers_provisioning_and_closes_before_success(self):
        def issue():
            self.native.Progress.assert_called_once()
            self.context.__exit__.assert_not_called()
            return 'synthetic-key'
        self.assertEqual(work_account._waiting(issue), 'synthetic-key')
        self.context.__exit__.assert_called_once()
        text = self.native.Progress.call_args.args[0]
        self.assertIn('browser', text)
        self.assertIn('Cancel stops setup', text)
        self.assertNotIn('synthetic-key', text)

    def test_cancel_before_request_does_not_provision(self):
        self.progress.process.poll.return_value = 0
        issue = mock.Mock()
        self.assertIs(work_account._waiting(issue), work_account.CANCELLED)
        issue.assert_not_called()

    def test_cancel_during_context_exit_discards_prepared_key(self):
        self.context.__exit__.side_effect = lambda *args: setattr(self.progress, 'cancelled', True)
        self.assertIs(work_account._waiting(lambda: 'synthetic-key'), work_account.CANCELLED)

    def test_cancel_during_request_discards_late_key_without_waiting_for_it(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def issue():
            started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError('test request was not released')
                return 'synthetic-key'
            finally:
                finished.set()
        self.progress.process.poll.side_effect = lambda: 0 if started.is_set() else None
        try:
            self.assertIs(work_account._waiting(issue), work_account.CANCELLED)
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        self.context.__exit__.assert_called_once()

    def test_error_is_shown_after_wait_cleanup_and_never_exposes_credentials(self):
        issue = mock.Mock(side_effect=ValueError('synthetic-private-key'))
        with self.assertRaises(work_account.SignInFailed) as caught:
            work_account._waiting(issue)
        self.context.__exit__.assert_called_once()
        self.assertNotIn('synthetic-private-key', str(caught.exception))

    def test_provisioning_error_keeps_the_existing_actionable_message(self):
        issue = mock.Mock(side_effect=work_account.SignInFailed('Contact IT.'))
        with self.assertRaisesRegex(work_account.SignInFailed, 'Contact IT'):
            work_account._waiting(issue)

    def test_unavailable_waiting_window_is_an_actionable_failure(self):
        self.context.__enter__.side_effect = OSError('synthetic-private-value')
        issue = mock.Mock()
        with self.assertRaises(work_account.SignInFailed) as caught:
            work_account._waiting(issue)
        self.assertIn('window could not open', str(caught.exception))
        self.assertNotIn('synthetic-private-value', str(caught.exception))
        issue.assert_not_called()

    def test_helper_failure_is_not_reported_as_user_cancellation(self):
        self.progress.process.poll.return_value = 2
        issue = mock.Mock()
        with self.assertRaisesRegex(work_account.SignInFailed, 'window could not open'):
            work_account._waiting(issue)
        issue.assert_not_called()

    def test_default_provisioning_gets_a_cooperative_cancellation_signal(self):
        seen = []
        def provision(*, cancelled):
            seen.append(cancelled)
            self.assertFalse(cancelled())
            return 'synthetic-key'
        with mock.patch.object(work_account, 'provision', side_effect=provision):
            self.assertEqual(work_account._waiting(), 'synthetic-key')
        self.assertTrue(seen[0]())

    def test_cancel_signal_precedes_progress_cleanup_and_blocks_new_provisioning(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        signal = []
        post = mock.Mock()
        original = work_account.provision
        def authorize(config):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test authorization was not released')
            return {'access_token': 'synthetic-access'}
        def provision(*, cancelled):
            signal.append(cancelled)
            try:
                return original(config={'scope': 'synthetic', 'provision_url': 'https://example.invalid'},
                                authorize=authorize, post=post, cancelled=cancelled)
            finally:
                finished.set()
        def cleanup(*args):
            self.assertTrue(signal[0]())
            release.set()
            self.assertTrue(finished.wait(2))
        self.progress.process.poll.side_effect = lambda: 0 if entered.is_set() else None
        self.context.__exit__.side_effect = cleanup
        oauth = SimpleNamespace(SignInRequired=RuntimeError)
        try:
            with mock.patch.object(work_account, '_load',
                    side_effect=lambda name: self.native if name == 'native_dialog' else oauth), \
                    mock.patch.object(work_account, 'provision', side_effect=provision):
                self.assertIs(work_account._waiting(), work_account.CANCELLED)
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        post.assert_not_called()

    def test_cancelled_wait_never_asks_subscription_questions_or_reports_success(self):
        dialog = mock.Mock()
        dialog.answer.return_value = 'work'
        with mock.patch.object(work_account, '_load', return_value=dialog), \
                mock.patch.object(work_account, '_ask', return_value='work') as ask, \
                mock.patch.object(work_account, '_waiting', return_value=work_account.CANCELLED):
            self.assertIs(work_account.collect('/dialog', 'win32', 'codex'), work_account.CANCELLED)
        ask.assert_called_once()
        with mock.patch.object(work_account, 'collect', return_value=work_account.CANCELLED), \
                mock.patch.object(key_prompt, 'submit') as submit, \
                mock.patch.object(key_prompt.key_dialog, 'message') as message:
            self.assertEqual(key_prompt.run_window('http://127.0.0.1:1/t/setup/n',
                tool_path='/dialog', platform_name='win32', work=True), 2)
        submit.assert_not_called()
        message.assert_not_called()


class RealProgressWaitingTests(unittest.TestCase):
    @contextlib.contextmanager
    def progress_exit(self, final_poll):
        native = module('native_dialog')
        child = mock.Mock()
        child.poll.side_effect = [None, None, None, final_poll]
        with mock.patch.object(native, 'sys', SimpleNamespace(platform='win32')), \
                mock.patch.object(native.platform._windows_dialogs, 'progress', return_value=child), \
                mock.patch.dict(os.environ, {'ROUTER_HOME': 'synthetic-home'}), \
                mock.patch.object(work_account, '_load', return_value=native), \
                mock.patch.object(work_account.threading, 'Thread',
                                  side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            yield child

    def test_real_exit_records_user_exit_between_polls_but_not_parent_termination(self):
        for final_poll in (0, None):
            with self.subTest(final_poll=final_poll), self.progress_exit(final_poll) as child:
                result = work_account._waiting(lambda: 'synthetic-key')
                if final_poll == 0:
                    self.assertIs(result, work_account.CANCELLED)
                    child.terminate.assert_not_called()
                else:
                    self.assertEqual(result, 'synthetic-key')
                    child.terminate.assert_called_once()
                child.communicate.assert_called_once_with(timeout=5)

    def test_real_exit_cancellation_never_submits_the_prepared_key(self):
        with self.progress_exit(0), \
                mock.patch.object(work_account, 'provision', return_value='synthetic-key'), \
                mock.patch.object(work_account, '_ask', return_value='work') as ask, \
                mock.patch.object(key_prompt, 'submit', return_value=(200, {'ok': True})) as submit, \
                mock.patch.object(key_prompt.key_dialog, 'message') as message:
            self.assertEqual(key_prompt.run_window('http://127.0.0.1:1/t/setup/n',
                tool_path='/dialog', platform_name='win32', work=True), 2)
        ask.assert_called_once()
        submit.assert_not_called()
        message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
