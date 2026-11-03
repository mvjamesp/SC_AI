"""Vendor sealing changes routing representation, never restoration authority."""
import base64
import json
import unittest
from unittest import mock

from tests import test_attachment_classification as classification
from tests import test_claude_desktop_config as fixtures
from tests import test_claude_desktop_switch as switches
from tests import test_removal_recovery as removal
from tests.test_native_setup import module


def sealed(payload=b'synthetic opaque vendor data'):
    return '__sealed__:v1:' + base64.b64encode(payload).decode('ascii')


class SealedGatewayTests(unittest.TestCase):
    def setUp(self):
        self.rig = removal.RemovalRecoveryTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        self.owner = self.rig.install._desktop_config
        self.profile = fixtures._profile(self.rig.root, {'keep': 'untouched'})
        self.entry = self.owner.attach(self.rig.home, self.rig.state, self.rig.owner,
                                      data_dir_override=self.profile)
        self.receipt = self.rig.home / 'activation.json'
        self.config = self.profile / 'claude_desktop_config.json'
        self.config.write_text('{ "deploymentMode": "1p", "keep": true }\n')
        self.seal()

    def seal(self, value=None):
        doc = json.loads(self.entry.read_text())
        doc[self.owner.API_KEY_KEY] = value or sealed()
        self.entry.write_text(json.dumps(doc, indent=3) + '\n')

    def protected(self):
        return {path: path.read_bytes() for path in (
            self.entry, self.receipt, self.config, self.rig.config, self.rig.settings)}

    def test_startup_sealing_stays_operational_without_writes_or_restoration_grant(self):
        before = self.protected()
        for value in (sealed(), sealed(b'another opaque plaintext representation')):
            self.seal(value)
            before[self.entry] = self.entry.read_bytes()
            result = classification.report.classify(self.rig.home)
            self.assertEqual(result['clients']['claude-desktop'], {'classification': 'owned'})
            for hidden in (value, str(self.profile), self.rig.state['token']):
                self.assertNotIn(hidden, json.dumps(result))
            self.assertEqual(self.protected(), before)
            with self.assertRaisesRegex(SystemExit, 'connection_changed'):
                self.owner.detach(self.rig.home, self.rig.owner)
            with self.assertRaisesRegex(SystemExit, 'No assistants were detached'):
                self.rig.remove()
            self.assertEqual(self.protected(), before)
            self.rig.backend.uninstall.assert_not_called()

    def test_opaque_reattach_preserves_entry_receipt_and_subscription_selection_bytes(self):
        before = self.protected()
        for _ in range(2):
            self.assertEqual(self.owner.attach(self.rig.home, self.rig.state, self.rig.owner,
                                             data_dir_override=self.profile), self.entry)
            self.assertEqual(self.protected(), before)

    def test_fresh_opaque_attachment_is_not_adopted(self):
        self.rig.owner.drop_client(self.rig.home, 'claude-desktop')
        self.config.unlink()
        before = self.entry.read_bytes(), self.receipt.read_bytes()
        with self.assertRaises(SystemExit):
            self.owner.attach(self.rig.home, self.rig.state, self.rig.owner, data_dir_override=self.profile)
        self.assertEqual((self.entry.read_bytes(), self.receipt.read_bytes()), before)

    def test_startup_seal_after_merge_read_is_preserved_without_receipt_write(self):
        self.seal(self.owner.API_KEY_VALUE)
        original, changed = self.owner.merge_entry, None
        receipt_before = self.receipt.read_bytes()
        def seal_after_read(*args):
            nonlocal changed
            result = original(*args)
            self.seal()
            changed = self.entry.read_bytes()
            return result
        with mock.patch.object(self.owner, 'merge_entry', side_effect=seal_after_read):
            with self.assertRaises(OSError):
                self.owner.attach(self.rig.home, self.rig.state, self.rig.owner, data_dir_override=self.profile)
        self.assertEqual(self.entry.read_bytes(), changed)
        self.assertEqual(self.receipt.read_bytes(), receipt_before)

    def test_bad_receipt_route_format_and_documents_refuse_without_writes(self):
        meta = self.profile / 'configLibrary/_meta.json'
        state_path = self.rig.home / 'mixed-router-state.json'
        paths = (self.entry, self.receipt, self.config, meta, state_path)
        clean = {path: path.read_bytes() for path in paths}
        defects = ('provider', 'kind', 'port', 'token', 'raw-key', 'empty-seal', 'bad-base64',
                   'future-seal', 'bootstrap', 'mode', 'applied-id', 'hybrid', 'written',
                   'missing-written', 'snapshot-missing', 'snapshot-type', 'duplicate-entry',
                   'duplicate-receipt', 'duplicate-meta', 'duplicate-mode', 'duplicate-state',
                   'snapshot-duplicate', 'legacy-receipt')
        for defect in defects:
            with self.subTest(defect=defect):
                for path, data in clean.items():
                    path.write_bytes(data)
                doc = json.loads(self.entry.read_text())
                receipt = json.loads(self.receipt.read_text())
                entry = receipt['clients']['claude-desktop']
                if defect == 'provider': doc['inferenceProvider'] = 'other'
                if defect == 'kind': doc['inferenceCredentialKind'] = 'interactive'
                if defect == 'port': doc['inferenceGatewayBaseUrl'] = doc['inferenceGatewayBaseUrl'].replace('1234', '4321')
                if defect == 'token': doc['inferenceGatewayBaseUrl'] += '-changed'
                if defect == 'raw-key': doc['inferenceGatewayApiKey'] = 'synthetic foreign key'
                if defect == 'empty-seal': doc['inferenceGatewayApiKey'] = '__sealed__:v1:'
                if defect == 'bad-base64': doc['inferenceGatewayApiKey'] = '__sealed__:v1:!invalid!'
                if defect == 'future-seal': doc['inferenceGatewayApiKey'] = sealed().replace(':v1:', ':v2:')
                if defect == 'bootstrap': doc['bootstrapUrl'] = 'https://example.invalid/config'
                if defect == 'mode': self.config.write_text('{"deploymentMode":"other"}')
                if defect == 'applied-id': meta.write_text('{"appliedId":"different"}')
                if defect == 'hybrid': meta.write_text(json.dumps(dict(json.loads(meta.read_text()), hybridPointer=True)))
                if defect == 'written': entry['written']['inferenceGatewayApiKey'] = 'different'
                if defect == 'missing-written': entry.pop('written')
                if defect == 'legacy-receipt': receipt['version'] = 1
                if defect.startswith('snapshot-'):
                    snapshot = json.loads(entry['inserted_block'])
                    if defect == 'snapshot-missing': snapshot.pop('deploymentMode')
                    if defect == 'snapshot-type': snapshot['inferenceGatewayApiKey'] = 42
                    entry['inserted_block'] = json.dumps(snapshot)
                    if defect == 'snapshot-duplicate':
                        entry['inserted_block'] = entry['inserted_block'].replace('{', '{"deploymentMode":null,', 1)
                self.entry.write_text(json.dumps(doc))
                self.receipt.write_text(json.dumps(receipt))
                if defect == 'duplicate-entry': self.entry.write_text(self.entry.read_text().replace('{', '{"keep":0,', 1))
                if defect == 'duplicate-receipt': self.receipt.write_text(self.receipt.read_text().replace('{', '{"version":2,', 1))
                if defect == 'duplicate-meta': meta.write_text(meta.read_text().replace('{', '{"appliedId":"other",', 1))
                if defect == 'duplicate-mode': self.config.write_text('{"deploymentMode":"3p","deploymentMode":"1p"}')
                if defect == 'duplicate-state':
                    state_path.write_text(state_path.read_text().replace('{', '{"port":4321,', 1))
                before = {path: path.read_bytes() for path in paths}
                result = classification.report.classify(self.rig.home)
                self.assertNotEqual(result.get('clients', {}).get('claude-desktop'), {'classification': 'owned'})
                with self.assertRaises((SystemExit, ValueError)):
                    self.owner.attach(self.rig.home, self.rig.state, self.rig.owner, data_dir_override=self.profile)
                self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_change_during_operational_proof_is_preserved_and_refused(self):
        targets = (self.entry, self.receipt, self.config, self.profile / 'configLibrary/_meta.json',
                   self.rig.home / 'mixed-router-state.json')
        clean = {path: path.read_bytes() for path in targets}
        receipt = json.loads(self.receipt.read_text())['clients']['claude-desktop']
        check = self.owner.atomic_write.check_unchanged
        for target in targets:
            with self.subTest(target=target.name):
                for path, data in clean.items():
                    path.write_bytes(data)
                def change(path, expected):
                    if path == target:
                        path.write_bytes(clean[path] + b'\n')
                    return check(path, expected)
                with mock.patch.object(self.owner.atomic_write, 'check_unchanged', side_effect=change):
                    with self.assertRaises(OSError):
                        self.owner.operational_snapshot(self.rig.home, self.rig.state, receipt, self.rig.owner)
                self.assertEqual(target.read_bytes(), clean[target] + b'\n')

    def test_concurrent_receipt_change_is_preserved_and_refused(self):
        original = self.owner._owned_snapshot
        changed = None
        def change(*args):
            nonlocal changed
            result = original(*args)
            doc = json.loads(self.receipt.read_text())
            doc['clients']['claude-desktop']['written']['inferenceGatewayApiKey'] = 'changed'
            changed = json.dumps(doc)
            self.receipt.write_text(changed)
            return result
        before = self.entry.read_bytes()
        with mock.patch.object(self.owner, '_owned_snapshot', side_effect=change), self.assertRaises((SystemExit, ValueError)):
            self.owner.attach(self.rig.home, self.rig.state, self.rig.owner, data_dir_override=self.profile)
        self.assertEqual(self.entry.read_bytes(), before)
        self.assertEqual(self.receipt.read_text(), changed)

    def test_ready_routing_does_not_clear_an_independent_install_warning(self):
        state = dict(self.rig.state, clients='both', component_repairs={'assistants':'failed', 'scheduler':'failed'},
                     last_error='installer_failed', installation_repair_required=True)
        self.rig.install._write_state(self.rig.home, state)
        module('update_reconcile')._attention(self.rig.home, module('service'), 'unchanged')
        after = self.rig.install.ensure_state(self.rig.home)
        self.assertEqual(after['component_repairs'], {'assistants':'ready', 'scheduler':'failed'})
        self.assertTrue(after['installation_repair_required'])
        self.assertEqual(after['last_error'], 'installer_failed')


class SealedSwitchTests(unittest.TestCase):
    def setUp(self):
        self.rig = switches.SwitchTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        self.entry = self.rig.profile / 'configLibrary/11111111-2222-4333-8444-555555555555.json'
        doc = json.loads(self.entry.read_text())
        doc['inferenceGatewayApiKey'] = sealed()
        self.entry.write_text(json.dumps(doc))

    def test_sealed_switch_changes_only_mode(self):
        before = self.rig.files()
        for expected in ('1p', '3p'):
            self.rig.run.side_effect = [switches.result('button returned:Continue'),
                                       switches.result('stopped'), switches.result()]
            self.assertEqual(self.rig.switch(), expected)
            self.assertEqual(json.loads(self.rig.config.read_text())['deploymentMode'], expected)
            after = self.rig.files()
            self.assertEqual({p:v for p,v in after.items() if p != self.rig.config.relative_to(self.rig.root)},
                             {p:v for p,v in before.items() if p != self.rig.config.relative_to(self.rig.root)})

    def test_route_changed_during_sync_is_not_overwritten_or_selected(self):
        def changed(*args):
            doc = json.loads(self.entry.read_text())
            doc['inferenceGatewayBaseUrl'] += '-changed'
            self.entry.write_text(json.dumps(doc))
            return {'failed':0, 'pending':0}
        self.rig.sync.side_effect = changed
        before = self.rig.config.read_bytes(), (self.rig.home/'activation.json').read_bytes()
        with self.assertRaises(switches.switcher.SwitchError):
            self.rig.switch()
        self.assertTrue(json.loads(self.entry.read_text())['inferenceGatewayBaseUrl'].endswith('-changed'))
        self.assertEqual((self.rig.config.read_bytes(), (self.rig.home/'activation.json').read_bytes()), before)

    def test_settings_changed_during_final_policy_check_are_preserved(self):
        calls, changed = 0, None
        def policy():
            nonlocal calls, changed
            calls += 1
            if calls == 3:
                doc = json.loads(self.rig.config.read_text())
                doc['concurrentEdit'] = 'preserve this setting'
                changed = json.dumps(doc).encode()
                self.rig.config.write_bytes(changed)
            return False
        self.rig.policy.side_effect = policy
        before = self.entry.read_bytes(), (self.rig.home / 'activation.json').read_bytes()
        with self.assertRaises(switches.switcher.SwitchError):
            self.rig.switch()
        self.assertEqual(calls, 3)
        self.assertEqual(self.rig.config.read_bytes(), changed)
        self.assertEqual((self.entry.read_bytes(), (self.rig.home / 'activation.json').read_bytes()), before)
        self.assertEqual(self.rig.run.call_args.args[0], ['/usr/bin/open', '-b', switches.switcher.BUNDLE_ID])
