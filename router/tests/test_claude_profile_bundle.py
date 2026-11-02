"""The validated union and duplicates belong to one recoverable transaction."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import claude_profile_sync_fixtures as fixtures
from tests._platform import symlink_or_skip
from tests.claude_profile_sync_fixtures import PairFixture, SyncFixture, sync, load_helper


class ProfileBundleTests(PairFixture):
    def setUp(self):
        super().setUp()
        self.helper = load_helper('claude_profile_bundle')

    def consolidate(self):
        paths = sorted(self.store.glob('*/*/local_*.json'))
        duplicates = self.helper.duplicate_states(paths, self.record)
        inputs = [{'bundle': p.with_suffix(''), 'paths': (
            p.with_suffix(''), self.bundle, None, None)}
            for p in paths if p != self.record]
        stage = self.helper.stage_union(self.bundle, True, inputs, None)
        tx = self.helper._transaction
        manifest = tx.write_pair_manifest(self.store, self.record, self.bundle,
                                          self.record.read_text(), stage, duplicates=duplicates)
        try:
            tx.publish_pair(manifest, self.record.read_text())
        except Exception:
            tx.recover_store(self.store, errors=[])
            raise
        return len(duplicates)

    def test_duplicate_unique_content_and_empty_directories_join_before_publish(self):
        duplicate = self.duplicate()
        (duplicate.with_suffix('') / 'empty').mkdir()
        publisher = self.helper._transaction
        publish = publisher.publish_pair

        def inspect(manifest, text):
            doc = json.loads(manifest.read_text())
            stage = self.store / doc['stage']
            self.assertTrue((stage / 'unique.txt').exists())
            self.assertTrue((stage / 'empty').is_dir())
            self.assertEqual(len(doc['duplicates']), 1)
            self.assertTrue(duplicate.exists())
            return publish(manifest, text)

        with mock.patch.object(publisher, 'publish_pair', side_effect=inspect):
            self.assertEqual(self.consolidate(), 1)
        self.assertFalse(duplicate.exists())
        self.assertTrue((self.bundle / 'unique.txt').exists())
        self.assertTrue((self.bundle / 'empty').is_dir())

    def test_duplicate_task_files_join_the_union_with_exact_bytes(self):
        duplicate = self.duplicate()
        unique = duplicate.with_suffix('') / 'unique.json'
        unique.write_text(json.dumps({
            'accountName': 'task data', 'emailAddress': 'synthetic@example.invalid',
            'path': str(duplicate.with_suffix('') / 'unique.txt'), 'messages': []}, indent=2))
        raw = unique.read_bytes()
        self.assertEqual(self.consolidate(), 1)
        self.assertEqual((self.bundle / 'unique.json').read_bytes(), raw)
        self.assertFalse(duplicate.exists())

    def test_same_path_active_content_wins_and_shape_conflict_preserves_originals(self):
        duplicate = self.duplicate()
        (duplicate.with_suffix('') / 'outputs').mkdir()
        (duplicate.with_suffix('') / 'outputs/old.txt').write_text('inactive')
        self.assertEqual(self.consolidate(), 1)
        self.assertEqual((self.bundle / 'outputs/old.txt').read_text(), 'old')
        other = self.duplicate('shape')
        (other.with_suffix('') / 'outputs').write_text('only copy')
        with self.assertRaises(ValueError):
            self.consolidate()
        self.assertEqual((other.with_suffix('') / 'outputs').read_text(), 'only copy')
        self.assertTrue(other.exists())

    def test_excluded_duplicate_blocks_retirement(self):
        duplicate = self.duplicate()
        excluded = duplicate.with_suffix('') / 'password.txt'
        excluded.write_text('local only')
        with self.assertRaisesRegex(ValueError, 'excluded duplicate'):
            self.consolidate()
        self.assertEqual(excluded.read_text(), 'local only')
        self.assertTrue(duplicate.exists())

    def test_irregular_bundle_enumeration_skips_and_retirement_refuses(self):
        duplicate = self.duplicate()
        unsafe = duplicate.with_suffix('') / 'link'
        symlink_or_skip(self.record, unsafe)
        files = self.helper._files(duplicate.with_suffix(''))
        self.assertNotIn('link', files)
        self.assertIn('unique.txt', files)
        with self.assertRaises(ValueError):
            self.consolidate()
        self.assertTrue(duplicate.exists())
        self.assertTrue(unsafe.is_symlink())

    def test_cleanup_failure_restores_every_duplicate_and_canonical_pair(self):
        duplicates = [self.duplicate('one'), self.duplicate('two')]
        pub = self.helper._transaction._publish
        move = pub.move_new

        def fail(source, target):
            if source == duplicates[-1].with_suffix(''):
                raise OSError('cleanup interrupted')
            move(source, target)

        with mock.patch.object(pub, 'move_new', side_effect=fail):
            with self.assertRaises(OSError):
                self.consolidate()
        for duplicate in duplicates:
            self.assertTrue(duplicate.exists())
            self.assertEqual((duplicate.with_suffix('') / 'unique.txt').read_text(), 'unique')
        self.assertEqual((self.bundle / 'outputs/old.txt').read_text(), 'old')

    def test_committed_cleanup_error_keeps_union_and_retries_owned_cleanup(self):
        duplicate = self.duplicate()
        pub = self.helper._transaction._publish
        with mock.patch.object(pub, 'finish', side_effect=PermissionError('cleanup')):
            with self.assertRaises(PermissionError):
                self.consolidate()
        self.assertEqual((self.bundle / 'unique.txt').read_text(), 'unique')
        self.helper._transaction.recover_store(self.store)
        self.assertFalse(duplicate.exists())
        self.assertFalse(list(self.store.glob('.claude-profile-transaction-*')))


class BundleSyncSafetyTests(SyncFixture):
    @unittest.skipUnless(os.name == 'nt', 'Windows extended-length filesystem paths')
    def test_long_transcript_sync_is_idempotent_and_cleans_unchanged_stages(self):
        cli = 'history-' + 'c' * 80
        record = fixtures.record(
            self.third, 'claude-code-sessions', '87654321-4321-4567-8123-123456789abc',
            'fedcbaab-4321-4567-8123-123456789abc', 'deep', cli=cli)
        relative = Path('.claude/projects/session') / (cli + '.jsonl')
        history = record.with_suffix('') / relative
        self.assertGreater(len(str(history)), 260)
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(shutil.rmtree, Path('\\\\?\\' + str(self.root)))
        native = Path('\\\\?\\' + str(history))
        native.parent.mkdir(parents=True)
        row = {'cwd': str(self.third / 'work'), 'message': {'content': 'Caf\u00e9 \u65e5\u672c'}}
        native.write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
        for _ in range(2):
            report = self.sync('3p')
            self.assertEqual((report['failed'], report['pending']), (0, 0), report)
        self.assertEqual((report['copied'], report['replaced']), (0, 0), report)
        for profile in (self.first, self.third):
            saved = self.paths(profile, 'claude-code-sessions', 'deep')[0].with_suffix('')
            copied = json.loads((Path('\\\\?\\' + str(saved)) / relative).read_bytes())
            self.assertEqual(copied['message'], row['message'])
            self.assertEqual(copied['cwd'], str(profile / 'work'))
            self.assertFalse(list(saved.parent.glob('*.tmp-*')))

    @unittest.skipUnless(os.name == 'nt', 'Windows byte-range coordination lock')
    def test_held_lock_keeps_presence_and_metadata_in_fingerprint(self):
        path = self.third / '.claude-profile-sync.lock'
        with sync._service.install_lock(self.third, path.name) as locked:
            self.assertTrue(locked)
            with self.assertRaises(PermissionError):
                path.read_bytes()
            before = fixtures.fingerprint(self.third)
            info = path.stat()
            self.assertIn((path.name, 'coordination-lock', info.st_size,
                           info.st_mtime_ns, info.st_ino, info.st_mode & 0o777), before)
            self.assertEqual(fixtures.fingerprint(self.third), before)

    def test_fingerprint_propagates_unreadable_user_files(self):
        read = Path.read_bytes
        for name in ('user.json', 'bundle/.claude-profile-sync.lock'):
            with self.subTest(name=name):
                path = self.third / name
                path.parent.mkdir(exist_ok=True)
                path.write_text('{}')

                def unreadable(item):
                    if item == path:
                        raise PermissionError('synthetic user file refusal')
                    return read(item)

                with mock.patch.object(Path, 'read_bytes', unreadable):
                    with self.assertRaises(PermissionError):
                        fixtures.fingerprint(self.third)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended-length filesystem paths')
    def test_long_bundle_staging_keeps_utf8_and_ordinary_metadata_paths(self):
        parent = self.root / ('profile-' + 'a' * 100) / ('sessions-' + 'b' * 100)
        self.assertIn(self.root, parent.parents)
        native = Path('\\\\?\\' + str(parent))
        native.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, Path('\\\\?\\' + str(parent.parent)))
        target = parent / 'local_task'
        source = self.root / 'source'
        history = source / '.claude/projects/session/cli.jsonl'
        history.parent.mkdir(parents=True)
        message = 'Caf\u00e9 \u65e5\u672c\u0085\u2028\u2029'
        row = {'cwd': str(source / 'outputs'), 'message': {'content': message}}
        history.write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
        artifact = source / 'outputs/exact.txt'
        artifact.parent.mkdir()
        raw = 'Caf\u00e9 \u65e5\u672c\r\n'.encode('utf-8')
        artifact.write_bytes(raw)
        stage = sync._stage_union(target, False, [{'bundle': source, 'paths': (
            source, target, self.root, self.root)}], history)
        self.assertGreater(len(str(stage)), 260)
        self.assertFalse(str(stage).startswith('\\\\?\\'))
        saved = Path('\\\\?\\' + str(stage))
        self.assertEqual((saved / 'outputs/exact.txt').read_bytes(), raw)
        copied = json.loads((saved / history.relative_to(source)).read_bytes())
        self.assertEqual(copied['message'], row['message'])
        self.assertEqual(copied['cwd'], str(target / 'outputs'))
        self.assertNotIn('\\\\?\\', copied['cwd'])

    def test_symlink_session_does_not_abort_valid_sessions(self):
        for store in sync.STORES:
            for identifier in ('link', 'valid'):
                record = fixtures.record(
                    self.third, store, '87654321-4321-4567-8123-123456789abc',
                    'fedcbaab-4321-4567-8123-123456789abc', identifier,
                    bundle_files={'safe.txt': 'safe'})
                if identifier == 'link':
                    symlink_or_skip(self.home, record.with_suffix('') / 'unsafe',
                                    target_is_directory=True)
        report = self.sync()
        self.assertEqual(report['failed'], len(sync.STORES), report)
        for store in sync.STORES:
            self.assertEqual(len(self.paths(self.first, store, 'valid')), 1)
            self.assertEqual(self.paths(self.first, store, 'link'), [])
            link = self.paths(self.third, store, 'link')[0].with_suffix('') / 'unsafe'
            self.assertTrue(link.is_symlink())
