"""A refused transition cannot reopen an earlier account's gateway tasks."""
import json
import os
import unittest
from unittest import mock

from tests import test_claude_account_switch as cases

FIRST, OTHER = cases.FIRST, cases.OTHER


class AccountReopenTests(unittest.TestCase):
    def setUp(self):
        self.rig = cases.AccountSwitchTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        self.rig.config.write_text('{"deploymentMode":"3p"}')

    def matching_account(self):
        path = self.rig.first / 'config.json'
        doc = json.loads(path.read_bytes())
        doc['lastKnownAccountUuid'] = FIRST
        path.write_text(json.dumps(doc))
        return path, doc

    def pending_after_receipt(self):
        rotation, receipts = cases.module('claude_profile_rotation'), self.rig.owner._receipts
        replace = receipts.replace_receipt
        def interrupted(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError('synthetic interruption after receipt publication')
        with mock.patch.object(self.rig.owner._accounts, '_rotation', return_value=rotation), \
                mock.patch.object(receipts, 'replace_receipt', side_effect=interrupted):
            with self.assertRaisesRegex(self.rig.owner.SwitchError, 'recovery is pending'):
                self.rig.switch()
        return rotation

    def test_failed_safe_selection_keeps_recovery_pending_until_retry(self):
        rotation = self.pending_after_receipt()
        self.rig.platform.reset_mock()
        with mock.patch.object(self.rig.owner._accounts, '_rotation', return_value=rotation):
            with mock.patch.object(rotation._desktop, 'write_json', side_effect=OSError('synthetic write failure')):
                with self.assertRaises(self.rig.owner.SwitchError):
                    self.rig.switch(action='start')
            self.assertTrue(rotation.pending(self.rig.home))
            self.rig.platform.open_app.assert_not_called()
            self.assertEqual(self.rig.switch(action='start'), '1p')
        self.assertFalse(rotation.pending(self.rig.home))
        self.assertEqual(self.rig.owner.attached_profile(self.rig.home)[1]['deploymentMode'], '1p')

    def test_recovery_does_not_report_an_unperformed_openrouter_switch(self):
        self.pending_after_receipt()
        self.rig.platform.reset_mock()
        with self.assertRaisesRegex(self.rig.owner.SwitchError, 'recovered.*subscription.*OpenRouter again'):
            self.rig.switch()
        self.assertEqual(self.rig.owner.attached_profile(self.rig.home)[1]['deploymentMode'], '1p')
        self.rig.platform.open_app.assert_called_once()

    def test_recovery_flush_uses_a_writable_untruncated_handle(self):
        rotation = self.pending_after_receipt()
        opened, flush, handles = rotation.Path.open, os.fsync, {}
        def track(path, mode='r', *args, **kwargs):
            stream = opened(path, mode, *args, **kwargs)
            if path == self.rig.config:
                info = os.fstat(stream.fileno())
                handles[stream.fileno()] = (info.st_dev, info.st_ino, stream.writable())
            return stream
        def windows_flush(fd):
            info, tracked = os.fstat(fd), handles.get(fd)
            if tracked and (info.st_dev, info.st_ino, False) == tracked:
                raise OSError('synthetic Windows flush requires write access')
            return flush(fd)
        with mock.patch.object(self.rig.owner._accounts, '_rotation', return_value=rotation), \
                mock.patch.object(rotation.Path, 'open', track), \
                mock.patch.object(os, 'fsync', side_effect=windows_flush):
            self.assertEqual(self.rig.switch(action='start'), '1p')
        self.assertEqual(json.loads(self.rig.config.read_bytes()), {})

    def test_safe_subscription_return_keeps_damaged_sharing_metadata_unchanged(self):
        accounts = self.rig.owner._accounts
        binding = self.rig.home / accounts.ACCOUNTS
        owner = self.rig.third / accounts.OWNER
        binding.write_text('{"interrupted":')
        owner.write_text('{"owner":')
        identity = cases.fixtures.sync._records._identity
        with mock.patch.object(identity, 'subscription', return_value=None), \
                mock.patch.object(self.rig.owner, '_sync_profiles') as share:
            self.assertEqual(self.rig.switch('1p'), '1p')
        share.assert_not_called()
        self.assertEqual(binding.read_text(), '{"interrupted":')
        self.assertEqual(owner.read_text(), '{"owner":')
        self.assertEqual(json.loads(self.rig.config.read_bytes())['deploymentMode'], '1p')
        self.rig.platform.open_app.assert_called_once()
        self.rig.platform.reset_mock()
        with self.assertRaises(self.rig.owner.SwitchError):
            self.rig.switch('3p')
        self.rig.platform.open_app.assert_not_called()

    def assert_recovery_reattaches(self, selection):
        doc = {'keep': 'unrelated setting'}
        if selection is not None:
            doc['deploymentMode'] = '3p'
        self.rig.config.write_text(json.dumps(doc))
        if selection == '1p':
            self.assertEqual(self.rig.switch('1p'), '1p')
        self.pending_after_receipt()
        self.assertEqual(self.rig.switch(action='start'), '1p')
        desktop, receipts = self.rig.owner._desktop, self.rig.owner._receipts
        state = json.loads((self.rig.home / 'mixed-router-state.json').read_bytes())
        desktop._removal.detach_json(self.rig.home, 'claude-desktop', receipts)
        desktop.attach(self.rig.home, state, receipts, data_dir_override=self.rig.third)
        self.assertEqual(self.rig.owner.attached_profile(self.rig.home)[1]['deploymentMode'], '1p')
        self.assertEqual(json.loads(self.rig.config.read_bytes()).get('keep'), 'unrelated setting')

    def test_recovered_openrouter_selection_can_be_removed_and_reattached(self):
        self.assert_recovery_reattaches('3p')

    def test_recovered_router_selected_subscription_can_be_removed_and_reattached(self):
        self.assert_recovery_reattaches('1p')

    def test_recovered_implicit_subscription_can_be_removed_and_reattached(self):
        self.assert_recovery_reattaches(None)

    def test_failed_isolation_keeps_an_earlier_gateway_closed(self):
        with mock.patch.object(self.rig.owner._accounts, 'rotate',
                               side_effect=ValueError('synthetic retention refusal')):
            with self.assertRaises(self.rig.owner.SwitchError):
                self.rig.switch()
        self.rig.platform.open_app.assert_not_called()

    def test_account_change_while_quitting_cannot_reopen_the_old_gateway(self):
        path, doc = self.matching_account()
        def changed(*_args, **_kwargs):
            doc['lastKnownAccountUuid'] = OTHER
            path.write_text(json.dumps(doc))
            return True
        self.rig.platform.request_stop.side_effect = changed
        with self.assertRaisesRegex(self.rig.owner.SwitchError, 'account changed'):
            self.rig.switch('1p')
        self.rig.platform.open_app.assert_not_called()
        self.assertEqual(json.loads(self.rig.config.read_bytes())['deploymentMode'], '3p')

    def test_late_account_change_after_sharing_cannot_reopen_the_old_gateway(self):
        path, doc = self.matching_account()
        def changed(*_args, **_kwargs):
            doc['lastKnownAccountUuid'] = OTHER
            path.write_text(json.dumps(doc))
            return {'failed': 0, 'pending': 0}
        with mock.patch.object(self.rig.owner, '_sync_profiles', side_effect=changed):
            with self.assertRaisesRegex(self.rig.owner.SwitchError, 'account changed'):
                self.rig.switch('1p')
        self.rig.platform.open_app.assert_not_called()


if __name__ == '__main__':
    unittest.main()
