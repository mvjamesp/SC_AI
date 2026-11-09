"""Unknown accounts stop sharing before any task mutation."""
from unittest import mock
import json
import unittest
import os
import errno
import sys
import subprocess
from pathlib import Path
from tests import claude_profile_sync_fixtures as fixtures
from tests import test_claude_profile_rotation as rotation_cases


RECOVERY_CRASH = r'''
import importlib.util, os, pathlib, sys
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
helper = pathlib.Path(sys.argv[1]); rotation = load('rotation_worker', helper)
receipts = load('receipts_worker', helper.with_name('codex_config.py'))
receipts._atomic_write = rotation._atomic
home, boundary = pathlib.Path(sys.argv[2]), sys.argv[3]
original = rotation._publish.move_new
count = 0
def move(source, target):
    global count
    if '.tmp-' in source.name:
        return original(source, target)
    count += 1
    if boundary == 'before-' + str(count): os._exit(73)
    original(source, target)
    if boundary == 'after-' + str(count): os._exit(73)
rotation._publish.move_new = move
original_remove = rotation._atomic.remove
def remove(path, **kwargs):
    if boundary == 'finish-before': os._exit(73)
    original_remove(path, **kwargs)
    if boundary == 'finish-after': os._exit(73)
rotation._atomic.remove = remove
rotation.recover(home, receipts)
'''

class UnknownAccountSyncTests(fixtures.SyncFixture):
    def test_clean_profile_owner_refuses_another_native_account_before_binding(self):
        identity = fixtures.sync._records._identity
        owner = self.third / identity.OWNER
        owner.write_text(json.dumps({'version': 1, 'account': identity.native_digest(
            self.first, '11111111-2222-4333-8444-555555555555')}))
        report = self.sync()
        self.assertEqual(report['errors'], ['account-binding-changed'])
        self.assertFalse((self.home / '.claude-profile-control-accounts.json').exists())

    def test_unknown_binding_stops_before_identity_can_resolve_later(self):
        identity = fixtures.sync._records._identity
        with mock.patch.object(identity, 'bind_profile_accounts', return_value=None), \
                mock.patch.object(fixtures.sync, '_sync_direction') as transfer, \
                mock.patch.object(fixtures.sync._ledger, 'retire_deleted') as retire:
            report = self.sync()
        self.assertEqual((report['failed'], report['pending']), (0, 1))
        self.assertEqual(report['errors'], ['identity-unresolved'])
        transfer.assert_not_called()
        retire.assert_not_called()


class RotationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.rig = rotation_cases.RotationTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        self.home, self.profile = self.rig.home, self.rig.profile

    def test_marker_publication_only_exposes_a_complete_json_generation(self):
        rotation, receipts = rotation_cases.rotation, rotation_cases.receipts
        marker = self.home / rotation.PENDING
        fdopen = rotation.os.fdopen
        def interrupted(fd, mode):
            stream = fdopen(fd, mode)
            class Partial:
                def __enter__(self): return self
                def __exit__(self, *_args): stream.close()
                def write(self, raw):
                    stream.write(raw[:len(raw) // 2])
                    raise OSError('synthetic interruption during staging')
            return Partial()
        with mock.patch.object(rotation.os, 'fdopen', side_effect=interrupted):
            with self.assertRaises(OSError):
                rotation._marker(self.home, b'{"complete":"synthetic-marker"}')
        self.assertFalse(marker.exists())
        self.assertEqual(rotation.recover(self.home, receipts), 'none')

    def test_replaced_profile_is_detected_before_clean_profile_publication(self):
        rotation = rotation_cases.rotation
        original = self.profile.with_name('original-kept')
        move = rotation._publish.move_new
        def swapped(source, target):
            if source == self.profile:
                source.rename(original)
                source.mkdir()
                (source / 'sentinel').write_text('competing profile')
            return move(source, target)
        with mock.patch.object(rotation._publish, 'move_new', side_effect=swapped):
            with self.assertRaisesRegex(ValueError, 'changed-state'):
                self.rig.rotate()
        self.assertTrue(rotation.pending(self.home))
        self.assertTrue(original.is_dir())
        self.assertEqual((self.rig.archives()[0] / 'profile/sentinel').read_text(), 'competing profile')

    def test_post_validation_edit_never_becomes_the_new_ownership_baseline(self):
        rotation = rotation_cases.rotation
        validate = rotation._desktop.operational_snapshot
        path = rotation._desktop.applied_entry_path(self.profile)
        def edited(*args, **kwargs):
            result = validate(*args, **kwargs)
            doc = json.loads(path.read_bytes())
            doc['inferenceProvider'] = 'a different route'
            path.write_text(json.dumps(doc))
            return result
        with mock.patch.object(rotation._desktop, 'operational_snapshot', side_effect=edited):
            with self.assertRaisesRegex(ValueError, 'changed-state'):
                self.rig.rotate()
        self.assertTrue(self.profile.is_dir())
        self.assertFalse((self.rig.archives()[0] / 'profile').exists())

    def test_whole_router_state_remains_authoritative_across_route_validation(self):
        rotation = rotation_cases.rotation
        validate = rotation._desktop.operational_snapshot
        path = self.home / rotation.STATE
        def edited(*args, **kwargs):
            doc = json.loads(path.read_bytes())
            doc['claude_mode'] = 'none'
            path.write_text(json.dumps(doc))
            return validate(*args, **kwargs)
        with mock.patch.object(rotation._desktop, 'operational_snapshot', side_effect=edited):
            with self.assertRaisesRegex(ValueError, 'changed-state'):
                self.rig.rotate()
        self.assertFalse((self.rig.archives()[0] / 'profile').exists())

    def test_cross_mount_controls_and_marker_need_no_cross_directory_rename(self):
        rotation = rotation_cases.rotation
        move = rotation._publish.move_new
        def cross_mount(source, target):
            if (self.home in source.parents) != (self.home in target.parents):
                raise OSError(errno.EXDEV, 'synthetic bind mount')
            return move(source, target)
        with mock.patch.object(rotation._publish, 'move_new', side_effect=cross_mount):
            self.rig.rotate()
        self.assertFalse(rotation.pending(self.home))
        self.assertFalse(self.rig.binding.exists())

    def test_cross_mount_recovery_finalization_is_repeatable(self):
        rotation = rotation_cases.rotation
        move = rotation._publish.move_new
        def cross_mount(source, target):
            if (self.home in source.parents) != (self.home in target.parents):
                raise OSError(errno.EXDEV, 'synthetic bind mount')
            return move(source, target)
        writer = rotation_cases.receipts.replace_receipt
        def interrupted(*args, **kwargs):
            writer(*args, **kwargs)
            raise OSError('synthetic interruption after receipt')
        with mock.patch.object(rotation._publish, 'move_new', side_effect=cross_mount):
            with mock.patch.object(rotation_cases.receipts, 'replace_receipt', side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.rig.rotate()
            self.assertEqual(rotation.recover(self.home, rotation_cases.receipts), 'restored')
            self.assertEqual(rotation.recover(self.home, rotation_cases.receipts), 'none')

    def test_final_directory_sync_error_keeps_rotation_and_recovery_pending(self):
        rotation = rotation_cases.rotation
        sync = rotation._publish._fsync_dir
        removing = False
        remove_marker = rotation._remove_marker
        def final(*args):
            nonlocal removing
            removing = True
            try:
                return remove_marker(*args)
            finally:
                removing = False
        def fail_final(path):
            if path == self.rig.home and removing:
                raise OSError('synthetic final directory sync failure')
            return sync(path)
        with mock.patch.object(rotation, '_remove_marker', side_effect=final), \
                mock.patch.object(rotation._publish, '_fsync_dir', side_effect=fail_final):
            with self.assertRaises(OSError):
                self.rig.rotate()
            self.assertTrue(rotation.pending(self.rig.home))
            with self.assertRaises(OSError):
                rotation.recover(self.rig.home, rotation_cases.receipts)
            self.assertTrue(rotation.pending(self.rig.home))
        self.assertEqual(rotation.recover(self.rig.home, rotation_cases.receipts), 'restored')
        self.assertFalse(rotation.pending(self.rig.home))

    def test_marker_unlink_is_final_commit_with_no_fallible_effect_afterwards(self):
        rotation = rotation_cases.rotation
        marker = self.rig.home / rotation.PENDING
        raw = b'{"synthetic":"commit-marker"}'
        marker.write_bytes(raw)
        sync = mock.Mock()
        remove = rotation._atomic.remove
        def commit(path, **kwargs):
            remove(path, **kwargs)
            sync.side_effect = OSError('must not sync after commit')
        with mock.patch.object(rotation._publish, '_fsync_dir', sync), \
                mock.patch.object(rotation._atomic, 'remove', side_effect=commit):
            rotation._remove_marker(self.rig.home, raw)
        sync.assert_called_once()
        self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX parent permissions')
    def test_shared_writable_profile_parent_refuses_before_any_archive_effect(self):
        rotation = rotation_cases.rotation
        self.profile.parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'unsafe-profile-parent'):
            self.rig.rotate()
        self.assertFalse(rotation.pending(self.home))
        self.assertEqual(self.rig.archives(), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX no-follow permission change')
    def test_created_archive_swap_never_changes_external_file_permissions(self):
        rotation = rotation_cases.rotation
        target = self.home / 'external-sentinel'
        target.write_text('untouched')
        target.chmod(0o755)
        archive = self.profile.parent / '.synthetic-swap'
        mkdir = rotation.Path.mkdir
        def swapped(path, *args, **kwargs):
            mkdir(path, *args, **kwargs)
            if path == archive:
                path.rmdir()
                path.symlink_to(target)
        with mock.patch.object(rotation.Path, 'mkdir', swapped):
            rotation._private_dir(archive)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    @unittest.skipUnless(os.name == 'nt', 'native Windows extended-path contract')
    def test_long_original_and_archived_profile_paths_remain_usable(self):
        rotation = rotation_cases.rotation
        artifact = self.profile.joinpath(*(['long-synthetic-component'] * 12), 'keep.txt')
        rotation.io_path(artifact.parent).mkdir(parents=True)
        rotation.io_path(artifact).write_text('synthetic retained content')
        self.rig.rotate()
        archive, = self.rig.archives()
        self.assertEqual(rotation.io_path(archive / 'profile' /
            artifact.relative_to(self.profile)).read_text(), 'synthetic retained content')


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS ACL contract')
class MacParentAclTests(unittest.TestCase):
    def setUp(self):
        self.rig = rotation_cases.RotationTests()
        self.rig.setUp()
        self.addCleanup(self.rig.doCleanups)
        self.permissions = rotation_cases.rotation._permissions

    def test_empty_and_deny_only_acls_are_accepted(self):
        self.permissions.require_private_parent(self.rig.profile.parent)
        parent = self.rig.root / 'deny-only'
        parent.mkdir(mode=0o700)
        result = subprocess.run(['/bin/chmod', '+a', 'everyone deny delete', str(parent)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.permissions.require_private_parent(parent)

    def test_extended_allow_on_restrictive_parent_refuses_without_effect(self):
        parent = self.rig.profile.parent
        result = subprocess.run(['/bin/chmod', '+a',
            'everyone allow add_subdirectory,delete_child', str(parent)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(parent.stat().st_mode & 0o022)
        with self.assertRaises(PermissionError):
            self.rig.rotate()
        self.assertFalse(rotation_cases.rotation.pending(self.rig.home))
        self.assertEqual(self.rig.archives(), [])

    def test_acl_lookup_failure_is_not_treated_as_an_empty_acl(self):
        class Function:
            def __init__(self, operation): self.operation = operation
            def __call__(self, *_args):
                import ctypes
                ctypes.set_errno(errno.EACCES)
                return 0 if self.operation == 'get' else -1
        api = type('Api', (), {name: Function('get' if name == 'acl_get_fd_np' else name)
            for name in ('acl_get_fd_np', 'acl_get_entry', 'acl_get_tag_type', 'acl_free')})()
        with self.assertRaises(PermissionError):
            self.permissions.require_private_parent(Path(self.rig.root), api=api)
