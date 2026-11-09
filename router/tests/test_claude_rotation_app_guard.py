"""Reopening Claude during retention must leave recoverable pending work."""
import unittest
from unittest import mock

from tests import test_claude_account_switch as cases


class RotationAppGuardTests(unittest.TestCase):
    def setUp(self):
        self.rig = cases.AccountSwitchTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        self.rotation = cases.module('claude_profile_rotation')

    def test_reopen_during_clean_profile_preparation_stops_before_displacement(self):
        rotation = self.rotation
        stage = rotation._stage
        original = self.rig.owner._receipts.receipt_path_for(self.rig.home).read_bytes()
        def reopened(*args, **kwargs):
            result = stage(*args, **kwargs)
            self.rig.platform.is_running.return_value = True
            return result
        with mock.patch.object(self.rig.owner._accounts, '_rotation', return_value=rotation), \
                mock.patch.object(rotation, '_stage', side_effect=reopened):
            with self.assertRaisesRegex(self.rig.owner.SwitchError, 'recovery is pending'):
                self.rig.switch()
        self.assertTrue(rotation.pending(self.rig.home))
        self.assertTrue((self.rig.third / 'ant-did').exists())
        self.assertEqual(self.rig.owner._receipts.receipt_path_for(self.rig.home).read_bytes(), original)
        self.rig.platform.open_app.assert_not_called()

    def test_reopen_during_receipt_publication_keeps_recovery_pending(self):
        receipts = self.rig.owner._receipts
        replace = receipts.replace_receipt
        def reopened(*args, **kwargs):
            result = replace(*args, **kwargs)
            self.rig.platform.is_running.return_value = True
            return result
        with mock.patch.object(self.rig.owner._accounts, '_rotation', return_value=self.rotation), \
                mock.patch.object(receipts, 'replace_receipt', side_effect=reopened):
            with self.assertRaisesRegex(self.rig.owner.SwitchError, 'recovery is pending'):
                self.rig.switch()
        self.assertTrue(self.rotation.pending(self.rig.home))
        self.rig.platform.open_app.assert_not_called()

    def test_real_local_policy_reader_follows_the_retained_original_during_rotation(self):
        policy = self.rig.owner._platform._policy
        self.rig.platform.policy_disabled.side_effect = lambda platform, **kw: policy.policy_disabled(
            platform, kw['profile'])
        with mock.patch.object(policy, 'managed_connection', return_value=False):
            self.assertEqual(self.rig.owner.switch(self.rig.home, target='3p',
                platform_ops=self.rig.platform), '3p')
        self.assertFalse(self.rotation.pending(self.rig.home))

    def test_real_local_policy_reader_recovers_when_the_original_path_is_missing(self):
        policy = self.rig.owner._platform._policy
        self.rig.platform.policy_disabled.side_effect = lambda platform, **kw: policy.policy_disabled(
            platform, kw['profile'])
        move = self.rotation._publish.move_new
        def interrupted(source, target):
            move(source, target)
            if source == self.rig.third:
                raise OSError('synthetic interruption after original retention')
        with mock.patch.object(policy, 'managed_connection', return_value=False):
            with mock.patch.object(self.rig.owner._accounts, '_rotation', return_value=self.rotation), \
                    mock.patch.object(self.rotation._publish, 'move_new', side_effect=interrupted):
                with self.assertRaisesRegex(self.rig.owner.SwitchError, 'recovery is pending'):
                    self.rig.owner.switch(self.rig.home, target='3p', platform_ops=self.rig.platform)
            self.assertFalse(self.rig.third.exists())
            self.assertEqual(self.rig.owner.switch(self.rig.home, action='start',
                platform_ops=self.rig.platform), '1p')
        self.assertFalse(self.rotation.pending(self.rig.home))


if __name__ == '__main__':
    unittest.main()
