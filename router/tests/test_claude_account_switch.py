"""Account-aware switching preserves old profiles and requires fresh consent."""
import hashlib
import json
from unittest import mock

from tests import claude_profile_sync_fixtures as fixtures
from tests.test_native_setup import module


FIRST = '12345678-1234-4567-8123-123456789abc'
OTHER = '11111111-2222-4333-8444-555555555555'
ORG = 'abcdefab-1234-4567-8123-123456789abc'


class AccountPreflightTests(fixtures.SyncFixture):
    def setUp(self):
        super().setUp()
        self.accounts = fixtures.load_helper('claude_profile_accounts')
        self.context = (fixtures.sync, self.env, (self.first, self.third))

    def plan(self, destination='3p'):
        return self.accounts.plan(self.home, self.context, destination, subscription=True)

    def bind_then_change_account(self):
        self.assertFalse(self.sync()['pending'])
        doc = json.loads((self.first / 'config.json').read_bytes())
        doc['lastKnownAccountUuid'] = OTHER
        (self.first / 'config.json').write_text(json.dumps(doc))
        fixtures.namespace(self.first, 'claude-code-sessions', OTHER, ORG)

    def test_changed_pair_has_a_read_only_rotation_or_safe_return_plan(self):
        self.bind_then_change_account()
        before = fixtures.fingerprint(self.root)
        self.assertTrue(self.plan('3p')['rotate'])
        self.assertFalse(self.plan('1p')['rotate'])
        self.assertTrue(self.plan('1p')['return_only'])
        self.assertEqual(fixtures.fingerprint(self.root), before)

    def test_unresolved_existing_gateway_is_not_first_launch(self):
        self.bind_then_change_account()
        (self.third / 'ant-did').unlink()
        fixtures.record(self.third, 'claude-code-sessions', OTHER, ORG, 'old-task')
        self.assertTrue(self.plan()['rotate'])

    def test_existing_unbound_gateway_tasks_are_retained_not_assigned(self):
        fixtures.record(self.third, 'claude-code-sessions', OTHER, ORG, 'unknown-owner')
        self.assertTrue(self.plan()['rotate'])

    def test_existing_gateway_identity_without_binding_is_not_first_use(self):
        before = fixtures.fingerprint(self.third)
        self.assertTrue(self.plan()['rotate'])
        self.assertTrue(self.plan('1p')['return_only'])
        self.assertEqual(fixtures.fingerprint(self.third), before)

    def test_unknown_native_with_saved_pair_refuses_without_reassignment(self):
        self.bind_then_change_account()
        before = fixtures.fingerprint(self.root)
        with mock.patch.object(fixtures.sync._records._identity, 'subscription', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Open a Code task'):
                self.plan()
        self.assertEqual(fixtures.fingerprint(self.root), before)

    def test_clean_profile_owner_pins_account_before_gateway_identity_exists(self):
        (self.third / 'ant-did').unlink()
        (self.third / 'config.json').unlink()
        owner = self.plan()['native']
        self.accounts.claim(self.third, owner)
        self.assertFalse(self.plan()['rotate'])
        doc = json.loads((self.first / 'config.json').read_bytes())
        doc['lastKnownAccountUuid'] = OTHER
        (self.first / 'config.json').write_text(json.dumps(doc))
        self.assertTrue(self.plan()['rotate'])

    def test_unknown_pair_cannot_claim_an_existing_profile(self):
        identity = fixtures.sync._records._identity
        before = fixtures.fingerprint(self.root)
        with mock.patch.object(identity, 'subscription', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Open a Code task'):
                self.plan()
        self.assertEqual(fixtures.fingerprint(self.root), before)

    def test_unsafe_owner_metadata_is_not_treated_as_a_fresh_profile(self):
        marker = self.third / self.accounts.OWNER
        marker.write_text('{"version":1,"account":"not-a-digest"}')
        with self.assertRaisesRegex(ValueError, 'profile ownership'):
            self.plan()

    def test_claim_is_exclusive_and_refuses_a_different_account(self):
        value = hashlib.sha256(b'synthetic-account').hexdigest()
        self.accounts.claim(self.third, value)
        before = fixtures.fingerprint(self.third)
        self.accounts.claim(self.third, value)
        with self.assertRaises(ValueError):
            self.accounts.claim(self.third, hashlib.sha256(b'another-account').hexdigest())
        self.assertEqual(fixtures.fingerprint(self.third), before)

    def test_openrouter_only_keeps_unshared_existing_profile_without_a_subscription(self):
        self.sync()
        before = fixtures.fingerprint(self.third)
        with mock.patch.object(fixtures.sync._records._identity, 'subscription', return_value=None):
            plan = self.accounts.plan(self.home, self.context, '3p', subscription=False)
        self.assertFalse(plan['rotate'])
        self.assertEqual(fixtures.fingerprint(self.third), before)

    def test_openrouter_only_ignores_a_readable_dormant_native_identity(self):
        before = fixtures.fingerprint(self.third)
        plan = self.accounts.plan(self.home, self.context, '3p', subscription=False)
        self.assertFalse(plan['rotate'])
        self.assertIsNone(plan['native'])
        self.assertEqual(fixtures.fingerprint(self.third), before)



class AccountSwitchTests(fixtures.SyncFixture):
    def setUp(self):
        super().setUp()
        self.owner = module('claude_desktop_switch')
        self.owner._receipts._atomic_write = module('atomic_write')
        state = dict(port=1, token='synthetic', openai_models=[], claude_mode='subscription')
        (self.home / 'mixed-router-state.json').write_text(json.dumps(state))
        entry = self.owner._desktop.applied_entry_path(self.third)
        doc = json.loads(entry.read_bytes())
        for key in self.owner._desktop.OWNED_KEYS:
            doc.pop(key, None)
        entry.write_text(json.dumps(doc))
        self.owner._desktop.attach(self.home, state, self.owner._receipts,
                                   data_dir_override=self.third)
        self.config = self.third / 'claude_desktop_config.json'
        self.config.write_text('{"deploymentMode":"1p","keep":"setting"}')
        self.platform = mock.Mock()
        self.platform.confirm.return_value = True
        self.platform.request_stop.return_value = True
        self.platform.is_running.return_value = False
        self.platform.open_app.return_value = None
        context = mock.patch.object(self.owner, '_sync_context',
            return_value=(fixtures.sync, self.env, (self.first, self.third)))
        context.start()
        self.addCleanup(context.stop)
        self.sync()
        doc = json.loads((self.first / 'config.json').read_bytes())
        doc['lastKnownAccountUuid'] = OTHER
        (self.first / 'config.json').write_text(json.dumps(doc))
        fixtures.namespace(self.first, 'claude-code-sessions', OTHER, ORG)

    def switch(self, target='3p', action='switch'):
        return self.owner.switch(self.home, target=target, action=action,
            policy_check=lambda: False, platform_ops=self.platform)

    def test_cancel_account_recovery_never_quits_or_changes_profile(self):
        self.platform.confirm.return_value = False
        before = fixtures.fingerprint(self.third)
        self.assertIsNone(self.switch())
        self.platform.request_stop.assert_not_called()
        self.assertIn('separate OpenRouter profile', self.platform.confirm.call_args.args[0])
        self.assertEqual(fixtures.fingerprint(self.third), before)

    def test_return_to_subscription_never_shares_old_gateway_tasks(self):
        self.config.write_text('{"deploymentMode":"3p"}')
        before = (self.home / '.claude-profile-control-accounts.json').read_bytes()
        with mock.patch.object(self.owner, '_sync_profiles') as share:
            self.assertEqual(self.switch('1p'), '1p')
        share.assert_not_called()
        self.assertEqual((self.home / '.claude-profile-control-accounts.json').read_bytes(), before)
        self.assertIn('without sharing', self.platform.confirm.call_args.args[0])

    def test_account_change_during_restart_cannot_use_original_confirmation(self):
        # Start from a matching pair; the change happens after ordinary consent.
        doc = json.loads((self.first / 'config.json').read_bytes())
        doc['lastKnownAccountUuid'] = FIRST
        (self.first / 'config.json').write_text(json.dumps(doc))
        def change_account(*_args, **_kwargs):
            doc['lastKnownAccountUuid'] = OTHER
            (self.first / 'config.json').write_text(json.dumps(doc))
            return True
        self.platform.request_stop.side_effect = change_account
        with self.assertRaisesRegex(self.owner.SwitchError, 'account changed'):
            self.switch()
        self.assertEqual(json.loads(self.config.read_bytes())['deploymentMode'], '1p')
        self.assertFalse(list(self.third.parent.glob('.Claude-3p*')))

    def test_normal_open_or_selected_openrouter_cannot_bypass_account_recovery(self):
        self.config.write_text('{"deploymentMode":"3p"}')
        self.platform.confirm.return_value = False
        for action in ('start', 'switch'):
            self.platform.reset_mock()
            self.assertIsNone(self.switch(action=action))
            self.platform.confirm.assert_called_once()
            self.platform.open_app.assert_not_called()

    def test_account_recovery_archives_tasks_and_selects_a_clean_gateway(self):
        fixtures.record(self.third, 'claude-code-sessions', OTHER, ORG, 'must-stay-old',
                        bundle_files={'outputs/keep.txt': 'retained synthetic work'})
        binding = (self.home / '.claude-profile-control-accounts.json').read_bytes()
        with mock.patch.object(self.owner, '_sync_profiles') as share:
            self.assertEqual(self.switch(), '3p')
        share.assert_not_called()
        self.assertEqual(json.loads(self.config.read_bytes())['deploymentMode'], '3p')
        self.assertFalse((self.third / 'ant-did').exists())
        self.assertFalse((self.third / 'claude-code-sessions').exists())
        self.assertFalse((self.home / '.claude-profile-control-accounts.json').exists())
        archives = list(self.third.parent.glob('.Claude-3p.rotation-*.archive'))
        self.assertEqual(len(archives), 1)
        self.assertIn(binding, [path.read_bytes() for path in archives[0].rglob('*') if path.is_file()])

    def test_interrupted_rotation_never_reopens_then_retry_restores_safely(self):
        rotation = module('claude_profile_rotation')
        publish = rotation._publish.move_new
        def interrupt(source, target):
            publish(source, target)
            if source == self.third:
                raise OSError('synthetic interruption after old profile retention')
        with mock.patch.object(self.owner._accounts, '_rotation', return_value=rotation), \
                mock.patch.object(rotation._publish, 'move_new', side_effect=interrupt):
            with self.assertRaisesRegex(self.owner.SwitchError, 'recovery is pending'):
                self.switch()
        self.platform.open_app.assert_not_called()
        self.assertTrue(rotation.pending(self.home))
        self.assertFalse(self.third.exists())
        self.assertEqual(self.switch(action='start'), '1p')
        self.assertFalse(rotation.pending(self.home))
        self.assertTrue(self.third.exists())
        self.platform.open_app.assert_called_once()
        self.assertEqual(self.owner.attached_profile(self.home)[1]['deploymentMode'], '1p')
        self.assertNotIn('deploymentMode', json.loads(self.config.read_bytes()))

    def test_recovery_locks_old_root_not_already_published_clean_root(self):
        rotation = module('claude_profile_rotation')
        publish = rotation._publish.move_new
        def interrupt(source, target):
            publish(source, target)
            if target == self.third:
                raise OSError('synthetic interruption after clean profile publication')
        with mock.patch.object(self.owner._accounts, '_rotation', return_value=rotation), \
                mock.patch.object(rotation._publish, 'move_new', side_effect=interrupt):
            with self.assertRaisesRegex(self.owner.SwitchError, 'recovery is pending'):
                self.switch()
        self.assertFalse((self.third / '.claude-profile-sync.lock').exists())
        self.assertEqual(self.switch(action='start'), '1p')
        self.assertFalse(rotation.pending(self.home))

    def test_cancel_interrupted_recovery_leaves_both_profiles_closed_and_retained(self):
        marker = self.home / '.claude-profile-rotation-pending.json'
        marker.write_text('{"invalid":"synthetic interruption"}')
        self.platform.confirm.return_value = False
        before = fixtures.fingerprint(self.third)
        self.assertIsNone(self.switch(action='start'))
        self.platform.request_stop.assert_not_called()
        self.platform.open_app.assert_not_called()
        self.assertEqual(fixtures.fingerprint(self.third), before)

    def test_setup_and_removal_cannot_bypass_pending_profile_recovery(self):
        (self.home / '.claude-profile-rotation-pending.json').write_text('{"pending":true}')
        state = json.loads((self.home / 'mixed-router-state.json').read_bytes())
        before = fixtures.fingerprint(self.third)
        with self.assertRaisesRegex(SystemExit, 'recover the saved profile'):
            self.owner._desktop.attach(self.home, state, self.owner._receipts,
                                       data_dir_override=self.third)
        removal = self.owner._desktop._removal
        entry = self.owner._receipts.read_receipt(self.home)['clients']['claude-desktop']
        with self.assertRaisesRegex(ValueError, 'profile_recovery_pending'):
            removal.json_plan(self.home, 'claude-desktop', entry, self.owner._receipts)
        self.assertEqual(fixtures.fingerprint(self.third), before)
