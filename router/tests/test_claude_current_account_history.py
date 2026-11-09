"""Only transcripts referenced by the verified account are shared."""
from tests import claude_profile_sync_fixtures as fixtures


FIRST = '12345678-1234-4567-8123-123456789abc'
ORG = 'abcdefab-1234-4567-8123-123456789abc'


class CurrentAccountHistoryTests(fixtures.SyncFixture):
    def test_new_pair_only_shares_history_referenced_by_its_current_account(self):
        identity = fixtures.sync._records._identity
        accounts = fixtures.load_helper('claude_profile_accounts')
        accounts.claim(self.third, identity.native_digest(self.first, FIRST))
        fixtures.record(self.first, 'claude-code-sessions', FIRST, ORG,
            'new-task', cli='current-cli',
            bundle_files={'outputs/keep.txt': 'new account output'})
        project = fixtures.Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects/workspace'
        project.mkdir(parents=True)
        transcript = project / 'old-cli.jsonl'
        transcript.write_text('{"message":"an earlier account transcript"}\n')
        before = transcript.read_bytes()
        (project / 'current-cli.jsonl').write_text('{"message":"current account task history"}\n')
        report = self.sync('1p')
        self.assertFalse(report['failed'], report)
        copied = self.paths(self.third, 'claude-code-sessions', 'new-task')[0]
        self.assertIn('current account', (copied.with_suffix('') /
            '.claude/projects/session/current-cli.jsonl').read_text())
        self.assertFalse(list(self.third.rglob('old-cli.jsonl')))
        self.assertEqual(transcript.read_bytes(), before)
