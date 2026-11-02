"""Interrupted cleanup and history repair resume on the next switch instead of blocking."""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests import claude_profile_sync_fixtures as fixtures
from tests.claude_profile_sync_fixtures import PairFixture, SyncFixture
from tests.test_claude_profile_history import cleanup_history_root, history, long_history, raw_path

A1, O1 = '12345678-1234-4567-8123-123456789abc', 'abcdefab-1234-4567-8123-123456789abc'
CLI = '11111111-2222-4333-8444-555555555555'


class CommittedCleanupTests(PairFixture):
    def test_cleanup_that_stopped_partway_finishes_on_the_next_run(self):
        for name in ('old2.txt', 'old3.txt'):
            (self.bundle / 'outputs' / name).write_text(name)
        manifest, real_delete = self.manifest(), self.publish.delete_checked

        def stop_partway(path, expected, kind):
            if kind == 'tree':  # One file goes, then a scanner holds the next one.
                next(item for item in sorted(path.rglob('*')) if item.is_file()).unlink()
                raise PermissionError(13, 'locked')
            return real_delete(path, expected, kind)

        with mock.patch.object(self.publish, 'delete_checked', side_effect=stop_partway):
            with self.assertRaises(PermissionError):
                self.transaction.publish_pair(manifest, self.text)
        self.assertEqual(json.loads(self.record.read_text())['title'], 'new')
        errors = []
        self.assertEqual(self.transaction.recover_store(self.store, errors), 1)
        self.assertEqual(errors, [])
        self.assertFalse(manifest.exists())
        self.assertEqual(list(self.ns.glob('.claude-profile-control-*')), [])
        self.assertTrue((self.bundle / 'outputs/new.txt').exists())


class HistoryRepairTests(SyncFixture):
    def setUp(self):
        super().setUp()
        cleanup_history_root(self, self.root)

    def task(self):
        """A Code task whose live history holds a gateway reply, so a switch repairs it."""
        live = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects/-Users-me-app' / (CLI + '.jsonl')
        raw_path(live.parent).mkdir(parents=True)
        raw_path(live).write_text(''.join(json.dumps(row) + '\n' for row in (
            {'type': 'user', 'uuid': 'u1', 'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'uuid': 'a1', 'requestId': 'gen-1',
             'message': {'id': 'gen-a', 'role': 'assistant', 'content': []}})), encoding='utf-8')
        fixtures.record(self.first, 'claude-code-sessions', A1, O1, 'task', cli=CLI)
        return live

    def assertSwitches(self):
        report = self.sync('1p')
        self.assertEqual((report['failed'], report['pending']), (0, 0), report)

    def test_transcript_deleted_after_a_repair_does_not_block(self):
        live = self.task()
        self.assertSwitches()
        raw_path(live).unlink()  # Claude Code removed an old conversation.
        self.assertSwitches()

    def test_copied_or_restored_home_does_not_block(self):
        live = self.task()
        self.assertSwitches()
        for item in list(raw_path(live.parent).iterdir()):
            copy = item.with_name(item.name + '.copy')
            shutil.copy2(item, copy)  # Same bytes, mode and times; a new inode.
            os.replace(copy, item)
        self.assertSwitches()

    def test_writer_racing_the_repair_leaves_the_transcript_for_the_next_run(self):
        live, history, raced = self.task(), fixtures.sync._history, []
        real_move = history._publish.move_new

        def racing(source, target):
            if not raced and Path(source) == live:
                raced.append(True)
                with raw_path(live).open('a', encoding='utf-8') as out:  # A writer the switch did not see.
                    out.write(json.dumps({'type': 'user', 'uuid': 'u2', 'message': 'late'}) + '\n')
            return real_move(source, target)

        with mock.patch.object(history._publish, 'move_new', side_effect=racing):
            self.assertEqual(self.sync('1p')['failed'], 1)
        self.assertEqual(sorted(p.name for p in raw_path(live.parent).iterdir()), [live.name])
        self.assertSwitches()
        self.assertIn('late', raw_path(live).read_text(encoding='utf-8'))

    def test_journal_half_written_by_a_killed_process_does_not_block(self):
        live = self.task()
        stem = '.' + live.name + '.history-repair-' + 'a' * 64
        raw_path(live.parent / (stem + '.journal.k3x9q1ab.tmp')).write_text('{', encoding='utf-8')
        raw_path(live.parent / (stem + '.stage')).write_text('staged before the journal', encoding='utf-8')
        self.assertSwitches()
        self.assertFalse([p.name for p in raw_path(live.parent).iterdir() if p.name.startswith(stem)])

    def test_completion_record_half_written_by_a_killed_process_does_not_block(self):
        live, history = self.task(), fixtures.sync._history

        def killed(path, journal, document):
            raw_path(path.with_name(path.name + '.x1y2z3ab.tmp')).write_text('{', encoding='utf-8')
            raise OSError('killed while writing')

        with mock.patch.object(history, '_write_done', side_effect=killed):
            self.assertEqual(self.sync('1p')['failed'], 1)
        self.assertSwitches()
        self.assertFalse([p.name for p in raw_path(live.parent).iterdir() if p.name.endswith('.tmp')])


@unittest.skipUnless(os.name == 'nt', 'Windows extended-length history recovery')
class LongHistoryRecoveryTests(unittest.TestCase):
    def snapshot(self, path):
        return {item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                for item in raw_path(path.parent).iterdir()}

    def original(self, path):
        data = b'{"type":"assistant","requestId":"foreign","message":{"id":"external","content":"keep"}}\n'
        raw_path(path).write_bytes(data)
        return data

    def test_long_repair_recovers_each_durable_boundary(self):
        for boundary in ('journal', 'original', 'target', 'done'):
            with self.subTest(boundary=boundary):
                path = long_history(self)
                original = self.original(path)
                move, write = history._publish.move_new, history._write_journal

                def interrupt_move(source, target):
                    move(source, target)
                    if ((boundary == 'original' and target.suffix == '.original')
                            or (boundary == 'target' and target == path)):
                        raise SystemExit('synthetic process interruption')

                def interrupt_write(target, document):
                    write(target, document)
                    if target.suffix == '.' + boundary:
                        raise SystemExit('synthetic process interruption')

                with mock.patch.object(history._publish, 'move_new', interrupt_move), \
                        mock.patch.object(history, '_write_journal', interrupt_write):
                    with self.assertRaises(SystemExit):
                        history.repair_file(path)
                fresh = fixtures.load_helper('claude_profile_history')
                self.assertEqual(fresh.repair_file(path),
                                 'unchanged' if boundary == 'done' else 'repaired')
                self.assertIn(b'providerRequestId', raw_path(path).read_bytes())
                backup = next(raw_path(path.parent).glob('*.original'))
                self.assertEqual(backup.read_bytes(), original)
                journal = next(raw_path(path.parent).glob('*.journal'))
                self.assertEqual(json.loads(journal.read_bytes())['version'], 1)
                before = self.snapshot(path)
                self.assertEqual(fresh.repair_file(path), 'unchanged')
                self.assertEqual(self.snapshot(path), before)
                self.assertFalse(any(name.endswith(('.stage', '.tmp')) for name in before))

    def test_long_displacement_race_rolls_back_the_writers_exact_data(self):
        path = long_history(self)
        original = self.original(path)
        changed = original + b'{"type":"user","message":{"content":"late writer"}}\n'
        move = history._publish.move_new

        def racing(source, target):
            if source == path:
                raw_path(path).write_bytes(changed)
            move(source, target)

        with mock.patch.object(history._publish, 'move_new', racing):
            with self.assertRaises(ValueError):
                history.repair_file(path)
        self.assertEqual(raw_path(path).read_bytes(), changed)
        self.assertEqual(list(self.snapshot(path)), [path.name])
        fresh = fixtures.load_helper('claude_profile_history')
        self.assertEqual(fresh.repair_file(path), 'repaired')
        self.assertIn(b'late writer', raw_path(path).read_bytes())
        self.assertEqual(next(raw_path(path.parent).glob('*.original')).read_bytes(), changed)
        self.assertEqual(fresh.repair_file(path), 'unchanged')

    def test_long_mutated_journals_leave_all_artifacts_unchanged(self):
        for field, value in (('target', '../outside.json'), ('unknown', True), ('identity', {})):
            with self.subTest(field=field):
                path = long_history(self)
                self.original(path)
                write = history._write_journal

                def prepared(target, document):
                    write(target, document)
                    raise SystemExit('synthetic prepared interruption')

                with mock.patch.object(history, '_write_journal', prepared):
                    with self.assertRaises(SystemExit):
                        history.repair_file(path)
                journal = next(raw_path(path.parent).glob('*.journal'))
                doc = json.loads(journal.read_bytes())
                doc[field] = value
                journal.write_text(json.dumps(doc), encoding='utf-8')
                before = self.snapshot(path)
                with self.assertRaises(ValueError):
                    fixtures.load_helper('claude_profile_history').repair_file(path)
                self.assertEqual(self.snapshot(path), before)

    def test_long_deleted_completed_history_stays_missing_and_quiet(self):
        path = long_history(self)
        self.original(path)
        self.assertEqual(history.repair_file(path), 'repaired')
        raw_path(path).unlink()
        before = self.snapshot(path)
        for _ in range(2):
            self.assertEqual(fixtures.load_helper('claude_profile_history').repair_file(path), 'missing')
            self.assertEqual(self.snapshot(path), before)
