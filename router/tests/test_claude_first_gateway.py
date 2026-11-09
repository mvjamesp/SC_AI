"""A fresh gateway stays bound to its first verified native account."""
import json
import shutil
import unittest
from unittest import mock

from tests import test_claude_account_switch as cases


class FirstGatewayTests(unittest.TestCase):
    def setUp(self):
        self.rig = cases.AccountSwitchTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        share = mock.patch.object(self.rig.owner, '_sync_profiles',
            side_effect=lambda _home, mode, _third, _env: self.rig.sync(mode))
        share.start()
        self.addCleanup(share.stop)
        doc = json.loads((self.rig.first / 'config.json').read_bytes())
        doc['lastKnownAccountUuid'] = cases.FIRST
        (self.rig.first / 'config.json').write_text(json.dumps(doc))
        for path in self.rig.third.iterdir():
            if path.name not in ('configLibrary', 'claude_desktop_config.json'):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        for name in (self.rig.owner._accounts.ACCOUNTS, '.claude-profile-control-ledger.json'):
            (self.rig.home / name).unlink(missing_ok=True)

    def test_first_gateway_round_trip_shares_its_tasks_without_rotating_itself(self):
        self.assertEqual(self.rig.switch(), '3p')
        owner = self.rig.third / self.rig.owner._accounts.OWNER
        self.assertTrue(owner.is_file())
        identity = cases.fixtures.sync._records._identity
        self.assertEqual(json.loads(owner.read_bytes()), {'version': 1,
            'account': identity.native_digest(self.rig.first, cases.FIRST)})
        gateway = '87654321-4321-4567-8123-123456789abc'
        cases.fixtures.third_party_identity(self.rig.third, gateway, cases.ORG)
        cases.fixtures.record(self.rig.third, 'claude-code-sessions', gateway, cases.ORG,
            'gateway-task', bundle_files={'outputs/result.txt': 'keep current account work'})
        self.assertEqual(self.rig.switch('1p'), '1p')
        self.assertTrue(self.rig.paths(self.rig.first, 'claude-code-sessions', 'gateway-task'))
        self.assertEqual(self.rig.switch('3p'), '3p')
        self.assertFalse(list(self.rig.third.parent.glob('.Claude-3p.rotation-*.archive')))

    def test_first_gateway_owner_detects_a_later_native_account(self):
        self.assertEqual(self.rig.switch(), '3p')
        doc = json.loads((self.rig.first / 'config.json').read_bytes())
        doc['lastKnownAccountUuid'] = cases.OTHER
        (self.rig.first / 'config.json').write_text(json.dumps(doc))
        cases.fixtures.namespace(self.rig.first, 'claude-code-sessions', cases.OTHER, cases.ORG)
        self.rig.platform.confirm.return_value = False
        self.rig.platform.reset_mock()
        self.assertIsNone(self.rig.switch())
        self.assertIn('separate OpenRouter profile', self.rig.platform.confirm.call_args.args[0])
        self.rig.platform.open_app.assert_not_called()

    def test_unknown_native_first_launch_is_refused_then_verified_launch_reopens(self):
        identity = cases.fixtures.sync._records._identity
        with mock.patch.object(identity, 'subscription', return_value=None):
            with self.assertRaisesRegex(self.rig.owner.SwitchError, 'Open a Code task'):
                self.rig.switch()
        self.rig.platform.confirm.assert_not_called()
        self.rig.platform.request_stop.assert_not_called()
        self.rig.platform.open_app.assert_not_called()
        self.assertEqual(json.loads(self.rig.config.read_bytes())['deploymentMode'], '1p')
        self.assertEqual(self.rig.switch(), '3p')
        gateway = '87654321-4321-4567-8123-123456789abc'
        cases.fixtures.third_party_identity(self.rig.third, gateway, cases.ORG)
        cases.fixtures.record(self.rig.third, 'claude-code-sessions', gateway, cases.ORG,
            'first-task', bundle_files={'outputs/result.txt': 'keep first gateway work'})
        self.rig.platform.reset_mock()
        self.assertEqual(self.rig.switch(action='start'), '3p')
        self.rig.platform.confirm.assert_not_called()
        self.rig.platform.open_app.assert_called_once()
        self.assertTrue(self.rig.paths(self.rig.third, 'claude-code-sessions', 'first-task'))
        self.assertFalse(list(self.rig.third.parent.glob('.Claude-3p.rotation-*.archive')))

    def test_already_selected_first_gateway_is_pinned_before_open_or_same_target(self):
        self.rig.config.write_text('{"deploymentMode":"3p"}')
        self.assertEqual(self.rig.switch(action='start'), '3p')
        self.assertTrue((self.rig.third / self.rig.owner._accounts.OWNER).is_file())
        self.rig.platform.request_stop.assert_called_once()
        gateway = '87654321-4321-4567-8123-123456789abc'
        cases.fixtures.third_party_identity(self.rig.third, gateway, cases.ORG)
        cases.fixtures.record(self.rig.third, 'claude-code-sessions', gateway, cases.ORG,
            'first-open-task', bundle_files={'outputs/keep.txt': 'keep first open work'})
        self.rig.platform.reset_mock()
        self.assertEqual(self.rig.switch(action='start'), '3p')
        self.assertEqual(self.rig.switch(), '3p')
        self.rig.platform.confirm.assert_not_called()
        self.assertTrue(self.rig.paths(self.rig.third, 'claude-code-sessions', 'first-open-task'))
        self.assertFalse(list(self.rig.third.parent.glob('.Claude-3p.rotation-*.archive')))

    def test_failed_first_claim_never_reopens_an_unowned_selected_gateway(self):
        self.rig.config.write_text('{"deploymentMode":"3p"}')
        with mock.patch.object(self.rig.owner._accounts, 'claim', side_effect=OSError('synthetic disk full')):
            with self.assertRaises(self.rig.owner.SwitchError):
                self.rig.switch(action='start')
        self.rig.platform.open_app.assert_not_called()
        self.assertFalse((self.rig.third / self.rig.owner._accounts.OWNER).exists())
        self.rig.platform.reset_mock()
        self.assertEqual(self.rig.switch(action='start'), '3p')
        self.assertTrue((self.rig.third / self.rig.owner._accounts.OWNER).is_file())
        self.assertFalse(list(self.rig.third.parent.glob('.Claude-3p.rotation-*.archive')))

    def test_interrupted_actual_owner_publication_retries_without_archiving_empty_profile(self):
        self.rig.config.write_text('{"deploymentMode":"3p"}')
        rotation = cases.module('claude_profile_rotation')
        move = rotation._publish.move_new
        def interrupted(source, target):
            if self.rig.owner._accounts.OWNER in source.name:
                raise OSError('synthetic owner staging publication failure')
            return move(source, target)
        with mock.patch.object(self.rig.owner._accounts, '_rotation', return_value=rotation), \
                mock.patch.object(rotation._publish, 'move_new', side_effect=interrupted):
            with self.assertRaises(self.rig.owner.SwitchError):
                self.rig.switch(action='start')
        self.rig.platform.open_app.assert_not_called()
        staged, = self.rig.third.glob(self.rig.owner._accounts.OWNER + '.tmp-*')
        retained = staged.read_bytes()
        self.assertEqual(self.rig.switch(action='start'), '3p')
        self.assertTrue((self.rig.third / self.rig.owner._accounts.OWNER).exists())
        self.assertFalse(list(self.rig.third.parent.glob('.Claude-3p.rotation-*.archive')))
        self.assertEqual(staged.read_bytes(), retained)

    def test_unowned_initialized_gateway_is_retained_instead_of_claimed(self):
        gateway = '87654321-4321-4567-8123-123456789abc'
        cases.fixtures.third_party_identity(self.rig.third, gateway, cases.ORG)
        old = self.rig.third / 'earlier-account-state'
        old.write_text('synthetic retained profile data')
        self.assertEqual(self.rig.switch(), '3p')
        archive, = self.rig.third.parent.glob('.Claude-3p.rotation-*.archive')
        self.assertEqual((archive / 'profile' / old.name).read_text(),
                         'synthetic retained profile data')
        self.assertFalse(old.exists())
        self.assertFalse((self.rig.third / 'ant-did').exists())

    def test_openrouter_only_start_preserves_profile_with_a_dormant_native_login(self):
        state_path = self.rig.home / 'mixed-router-state.json'
        state = json.loads(state_path.read_bytes())
        state['claude_mode'] = 'none'
        state_path.write_text(json.dumps(state))
        gateway = '87654321-4321-4567-8123-123456789abc'
        cases.fixtures.third_party_identity(self.rig.third, gateway, cases.ORG)
        self.rig.config.write_text('{"deploymentMode":"3p"}')
        old = self.rig.third / 'existing-work'
        old.write_text('synthetic work stays here')
        self.assertEqual(self.rig.switch(action='start'), '3p')
        self.rig.platform.confirm.assert_not_called()
        self.rig.platform.open_app.assert_called_once()
        self.assertEqual(old.read_text(), 'synthetic work stays here')
        self.assertFalse((self.rig.third / self.rig.owner._accounts.OWNER).exists())

    def test_open_without_a_subscription_keeps_an_unset_selection_harmless(self):
        state_path = self.rig.home / 'mixed-router-state.json'
        state = json.loads(state_path.read_bytes())
        state['claude_mode'] = 'none'
        state_path.write_text(json.dumps(state))
        self.rig.config.write_text('{}')
        for action in ('start', 'switch'):
            with self.subTest(action=action):
                self.rig.platform.reset_mock()
                self.assertEqual(self.rig.switch(target='1p', action=action), '1p')
                self.rig.platform.confirm.assert_not_called()
                self.rig.platform.request_stop.assert_not_called()
                self.assertEqual(self.rig.platform.open_app.call_count, 1 if action == 'start' else 0)
                self.assertEqual(self.rig.config.read_text(), '{}')

    def test_no_subscription_open_ignores_broken_old_pair_metadata_without_changing_it(self):
        state_path = self.rig.home / 'mixed-router-state.json'
        state = json.loads(state_path.read_bytes())
        state['claude_mode'] = 'none'
        state_path.write_text(json.dumps(state))
        self.rig.config.write_text('{"deploymentMode":"3p"}')
        binding = self.rig.home / self.rig.owner._accounts.ACCOUNTS
        owner = self.rig.third / self.rig.owner._accounts.OWNER
        binding.write_text('{"interrupted":')
        owner.write_text('{"old-owner":')
        work = self.rig.third / 'existing-work'
        work.write_text('keep this gateway task')
        for action in ('start', 'switch'):
            self.rig.platform.reset_mock()
            self.assertEqual(self.rig.switch(action=action), '3p')
            self.rig.platform.confirm.assert_not_called()
            self.rig.platform.request_stop.assert_not_called()
            self.assertEqual(work.read_text(), 'keep this gateway task')
            self.assertEqual(binding.read_text(), '{"interrupted":')
            self.assertEqual(owner.read_text(), '{"old-owner":')


if __name__ == '__main__':
    unittest.main()
