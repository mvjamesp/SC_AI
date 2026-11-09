"""Retained opaque entries cannot inherit another macOS user's read grant."""
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_claude_removal_guards as cases
from tests import test_claude_profile_rotation as rotation_cases


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS retention ACL contract')
class RetainedEntryAclTests(unittest.TestCase):
    def setUp(self):
        self.case = cases.DesktopRemovalGuardsTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.rig = self.case.rig
        self.removal = self.rig.rig.install._delivery._load('client_removal')
        self.entry = self.rig.rig.owner.read_receipt(self.rig.rig.home)['clients']['claude-desktop']

    def grant(self, path, value):
        result = subprocess.run(['/bin/chmod', '+a', value, str(path)], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_inheritable_home_read_grant_refuses_before_any_retention_or_detach(self):
        before = self.rig.protected()
        self.grant(self.rig.rig.home, 'everyone allow list,search,read,readattr,readextattr,readsecurity,file_inherit')
        with self.assertRaises(PermissionError):
            self.removal.json_plan(self.rig.rig.home, 'claude-desktop', self.entry, self.rig.rig.owner)
        self.assertEqual(self.rig.protected(), before)
        self.assertFalse(list(self.rig.rig.home.glob('claude-desktop-before-*.json')))
        self.rig.rig.backend.uninstall.assert_not_called()

    def test_existing_retained_file_with_a_read_grant_is_not_accepted(self):
        files = self.removal.json_plan(self.rig.rig.home, 'claude-desktop', self.entry, self.rig.rig.owner)
        archive, _, raw = next(row for row in files if isinstance(row[2], bytes) and row[1] != row[2])
        archive.write_bytes(raw)
        archive.chmod(0o600)
        self.grant(archive, 'everyone allow read')
        before = self.rig.protected()
        with self.assertRaises(PermissionError):
            self.removal.json_plan(self.rig.rig.home, 'claude-desktop', self.entry, self.rig.rig.owner)
        self.assertEqual(self.rig.protected(), before)


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS rotation ACL contract')
class RotationHomeAclTests(unittest.TestCase):
    def setUp(self):
        self.rig = rotation_cases.RotationTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)

    def grant_home_read(self):
        answer = subprocess.run(['/bin/chmod', '+a',
            'everyone allow list,search,read,readattr,readextattr,readsecurity,file_inherit',
            str(self.rig.home)], capture_output=True)
        self.assertEqual(answer.returncode, 0)

    def test_rotation_refuses_inherited_home_read_before_creating_pending_files(self):
        self.grant_home_read()
        with self.assertRaises(PermissionError):
            self.rig.rotate()
        self.rig.assert_original()
        self.assertFalse(self.rig.archives())

    def test_pending_recovery_refuses_inherited_home_read_before_restoring_a_receipt(self):
        replace = rotation_cases.receipts.replace_receipt
        def interrupted(*args, **kwargs):
            replace(*args, **kwargs)
            raise OSError('synthetic interruption after receipt publication')
        with mock.patch.object(rotation_cases.receipts, 'replace_receipt', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.rig.rotate()
        receipt = self.rig.receipt_path.read_bytes()
        self.grant_home_read()
        with self.assertRaises(PermissionError):
            rotation_cases.rotation.recover(self.rig.home, rotation_cases.receipts)
        self.assertTrue(rotation_cases.rotation.pending(self.rig.home))
        self.assertEqual(self.rig.receipt_path.read_bytes(), receipt)


if __name__ == '__main__':
    unittest.main()
