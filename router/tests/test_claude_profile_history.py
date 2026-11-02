"""Foreign diagnostics must not be replayed as Anthropic response evidence."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.claude_profile_sync_fixtures import load_helper

history = load_helper('claude_profile_history')


def raw_path(path):
    """Independent fixture/inspection spelling; product calls receive ordinary paths."""
    path = Path(path).absolute()
    return Path('\\\\?\\' + str(path)) if os.name == 'nt' else path


def cleanup_history_root(case, root):
    root = Path(root).resolve()
    case.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
    case.addCleanup(shutil.rmtree, raw_path(root))


def long_history(case):
    temporary = tempfile.TemporaryDirectory()
    case.addCleanup(temporary.cleanup)
    root = Path(temporary.name).resolve()
    cleanup_history_root(case, root)
    path = root / 'cli/projects' / ('workspace-' + 'w' * 135) / ('history-' + 'h' * 65 + '.jsonl')
    case.assertGreater(len(str(path)), 260)
    raw_path(path.parent).mkdir(parents=True)
    return path


class HistoryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows extended-length history paths')
    def test_long_existing_history_repairs_and_quietly_repeats(self):
        path = long_history(self)
        message = {'id': 'foreign-id', 'content': 'Caf\u00e9 \u65e5\u672c\u0085\u2028\u2029'}
        row = {'type': 'assistant', 'requestId': 'foreign-request', 'message': message}
        original = (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
        raw_path(path).write_bytes(original)
        self.assertEqual(history.repair_file(path), 'repaired')
        actual = json.loads(raw_path(path).read_bytes())
        self.assertEqual(actual['message'], message)
        self.assertEqual(actual['providerRequestId'], row['requestId'])
        self.assertNotIn('requestId', actual)
        files = list(raw_path(path.parent).iterdir())
        backup = next(item for item in files if item.suffix == '.original')
        journal = next(item for item in files if item.suffix == '.journal')
        self.assertGreater(len(str(backup)[4:]), 260)
        self.assertEqual(backup.read_bytes(), original)
        history._publish._winacl.require_owner_only(backup)
        doc = json.loads(journal.read_bytes())
        self.assertEqual(doc['version'], 1)
        self.assertEqual(doc['target'], path.name)
        self.assertTrue(all('/' not in doc[key] and '\\' not in doc[key]
                            for key in ('target', 'backup', 'stage')))
        before = {item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in files}
        self.assertEqual(history.repair_file(path), 'unchanged')
        self.assertEqual({item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                          for item in raw_path(path.parent).iterdir()}, before)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended-length history paths')
    def test_long_mutated_original_is_refused_without_more_changes(self):
        path = long_history(self)
        raw_path(path).write_bytes(b'{"type":"assistant","requestId":"foreign","message":{"id":"external"}}\n')
        self.assertEqual(history.repair_file(path), 'repaired')
        backup = next(raw_path(path.parent).glob('*.original'))
        backup.write_bytes(b'competing backup writer')
        before = {item.name: item.read_bytes() for item in raw_path(path.parent).iterdir()}
        with self.assertRaises(ValueError):
            history.repair_file(path)
        self.assertEqual({item.name: item.read_bytes()
                          for item in raw_path(path.parent).iterdir()}, before)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended-length history paths')
    def test_long_hardlinked_history_is_refused_unchanged(self):
        path = long_history(self)
        original = b'{"type":"assistant","requestId":"foreign","message":{"id":"external"}}\n'
        raw_path(path).write_bytes(original)
        alias = path.with_name('alias.jsonl')
        os.link(raw_path(path), raw_path(alias))
        with self.assertRaises(ValueError):
            history.repair_file(path)
        self.assertEqual(raw_path(path).read_bytes(), original)
        self.assertEqual(raw_path(alias).read_bytes(), original)
        self.assertEqual(len(list(raw_path(path.parent).iterdir())), 2)

    def test_projection_preserves_content_and_ids_but_separates_foreign_diagnostics(self):
        rows = [
            {'type': 'assistant', 'requestId': 'req-native',
             'message': {'id': 'msg_native', 'content': [{'type': 'text', 'text': 'first'}]}},
            {'type': 'user', 'sessionId': 'same-session', 'cliSessionId': 'same-cli',
             'message': {'content': 'continue'}},
            {'type': 'assistant', 'requestId': 'req-external',
             'message': {'id': 'gen-provider', 'content': [
                 {'type': 'text', 'text': 'second'},
                 {'type': 'tool_use', 'id': 'call_1', 'name': 'example', 'input': {}}]}},
        ]
        text = ''.join(json.dumps(row) + '\n' for row in rows)
        projected = history.portable_history(text)
        actual = [json.loads(line) for line in projected.splitlines()]
        self.assertEqual(actual[:2], rows[:2])
        self.assertEqual(actual[2]['message'], rows[2]['message'])
        self.assertNotIn('requestId', actual[2])
        self.assertEqual(actual[2]['providerRequestId'], rows[2]['requestId'])
        self.assertEqual(history.portable_history(projected), projected)

    def test_native_transcript_is_byte_identical_and_conflicting_metadata_is_refused(self):
        text = '{"type":"assistant","requestId":"req-native","message":{"id":"msg_native"}}\n\n'
        self.assertEqual(history.portable_history(text), text)
        error = json.dumps({'type': 'assistant', 'isApiErrorMessage': True,
                            'requestId': 'failed-request', 'message': {'id': 'synthetic'}})
        self.assertEqual(history.portable_history(error), error)
        with self.assertRaises(ValueError):
            history.portable_history(json.dumps({'type': 'assistant', 'requestId': 'first',
                'providerRequestId': 'second', 'message': {'id': 'external'}}))

    def test_file_repair_preserves_transcript_and_is_idempotent(self):
        rows = [
            {'type': 'assistant', 'requestId': 'req-native',
             'message': {'id': 'msg_native', 'content': [{'type': 'text', 'text': 'kept'}]}},
            {'type': 'assistant', 'requestId': 'req-foreign',
             'sessionId': 'same-session', 'cliSessionId': 'same-cli',
             'message': {'id': 'foreign-response', 'content': [
                 {'type': 'text', 'text': 'also kept'},
                 {'type': 'tool_use', 'id': 'tool_1', 'name': 'synthetic', 'input': {}}]}},
        ]
        original = ''.join(json.dumps(row) + '\n' for row in rows).encode()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / 'session.jsonl'
            path.write_bytes(original)
            self.assertEqual(history.repair_file(path), 'repaired')
            actual = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(actual[0], rows[0])
            self.assertEqual(actual[1]['message'], rows[1]['message'])
            self.assertEqual(actual[1]['sessionId'], rows[1]['sessionId'])
            self.assertEqual(actual[1]['cliSessionId'], rows[1]['cliSessionId'])
            self.assertNotIn('requestId', actual[1])
            self.assertEqual(actual[1]['providerRequestId'], 'req-foreign')
            backups = list(Path(directory).glob('.session.jsonl.history-repair-*.original'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(history.repair_file(path), 'unchanged')
            self.assertEqual(backups[0].read_bytes(), original)

    def test_file_repair_recovers_after_source_was_displaced(self):
        original = json.dumps({'type': 'assistant', 'requestId': 'foreign',
            'message': {'id': 'provider-response', 'content': [{'type': 'text', 'text': 'kept'}]}}
            ).encode() + b'\n'
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / 'session.jsonl'
            path.write_bytes(original)
            real_move = history._publish.move_new
            interrupted = False

            def stop_after_displace(source, target):
                nonlocal interrupted
                real_move(source, target)
                if Path(source) == path and str(target).endswith('.original') and not interrupted:
                    interrupted = True
                    raise RuntimeError('simulated interruption')

            with mock.patch.object(history._publish, 'move_new', side_effect=stop_after_displace):
                with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                    history.repair_file(path)
            self.assertFalse(path.exists())
            backups = list(Path(directory).glob('.session.jsonl.history-repair-*.original'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(history.repair_file(path), 'repaired')
            self.assertEqual(history.repair_file(path), 'unchanged')
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertIn(b'providerRequestId', path.read_bytes())

    def test_file_repair_retains_a_competing_write_and_original(self):
        original = json.dumps({'type': 'assistant', 'requestId': 'foreign',
            'message': {'id': 'provider-response', 'content': [{'type': 'text', 'text': 'kept'}]}}
            ).encode() + b'\n'
        competing = b'{"type":"user","message":{"content":"concurrent"}}\n'
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / 'session.jsonl'
            path.write_bytes(original)
            real_move = history._publish.move_new

            def race_after_displace(source, target):
                real_move(source, target)
                if Path(source) == path and str(target).endswith('.original'):
                    with path.open('xb') as stream:
                        stream.write(competing)

            with mock.patch.object(history._publish, 'move_new', side_effect=race_after_displace):
                with self.assertRaises(ValueError):
                    history.repair_file(path)
            self.assertEqual(path.read_bytes(), competing)
            backups = list(Path(directory).glob('.session.jsonl.history-repair-*.original'))
            stages = list(Path(directory).glob('.session.jsonl.history-repair-*.stage'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(len(stages), 1)
            with self.assertRaises(ValueError):
                history.repair_file(path)
            self.assertEqual(path.read_bytes(), competing)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertTrue(stages[0].exists())

    def test_file_repair_handles_later_appends_as_new_generations(self):
        first = json.dumps({'type': 'assistant', 'requestId': 'foreign-1',
            'message': {'id': 'provider-response-1', 'content': 'first'}}).encode() + b'\n'
        second = json.dumps({'type': 'assistant', 'requestId': 'foreign-2',
            'message': {'id': 'provider-response-2', 'content': 'second'}}).encode() + b'\n'
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / 'session.jsonl'
            path.write_bytes(first)
            self.assertEqual(history.repair_file(path), 'repaired')
            first_repaired = path.read_bytes()
            path.write_bytes(first_repaired + second)
            before_second = path.read_bytes()
            self.assertEqual(history.repair_file(path), 'repaired')
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row['message']['id'] for row in rows],
                             ['provider-response-1', 'provider-response-2'])
            self.assertEqual([row['providerRequestId'] for row in rows],
                             ['foreign-1', 'foreign-2'])
            backups = list(Path(directory).glob('.session.jsonl.history-repair-*.original'))
            self.assertEqual(len(backups), 2)
            self.assertEqual({backup.read_bytes() for backup in backups}, {first, before_second})
            self.assertEqual(history.repair_file(path), 'unchanged')

    @unittest.skipUnless(hasattr(os, 'link'), 'hard links are unavailable')
    def test_file_repair_rejects_a_hard_link_without_changing_source(self):
        original = json.dumps({'type': 'assistant', 'requestId': 'foreign',
            'message': {'id': 'provider-response'}}).encode() + b'\n'
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / 'session.jsonl'
            alias = Path(directory) / 'alias.jsonl'
            path.write_bytes(original)
            os.link(path, alias)
            with self.assertRaises(ValueError):
                history.repair_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(alias.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
