"""Repair must reuse a working key while first setup can still collect one."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ai_router import key_prompt, work_account
from tests.test_native_setup import module, resources


class NativeKeyPromptTests(unittest.TestCase):
    def test_repair_skips_the_key_window_when_the_saved_key_is_usable(self):
        self.check_prompt(usable=True)

    def test_setup_still_opens_the_key_window_without_a_usable_key(self):
        self.check_prompt(usable=False)

    def test_already_reported_failure_retains_a_failure_outcome(self):
        self.check_prompt(usable=False, code=1)

    def check_prompt(self, usable, code=2):
        setup = module('native_setup')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = resources(root / 'download')
            home = root / 'home'
            home.mkdir()
            (home / 'current').write_text('1.2.0\n')
            key = home / 'openrouter.key'
            key.write_text('synthetic-saved-key' if usable else '')
            key.chmod(0o600)
            state = home / 'mixed-router-state.json'
            state.write_text(json.dumps(dict(clients='both', port=1, token='synthetic',
                                            setup=dict(nonce='test', expires=2000))))
            before = state.read_bytes()
            run = mock.Mock(return_value=SimpleNamespace(returncode=code))
            with mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic-shell-key'}), \
                    mock.patch.object(setup.time, 'time', return_value=1000):
                result = setup._key(source, home, run=run)
            if usable:
                self.assertEqual(result, 0)
                self.assertFalse(run.called, 'A usable saved key must skip the key window.')
            else:
                self.assertEqual(result, code)
                run.assert_called_once()
                self.assertIn('ai_router.key_prompt', run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs['env']['ROUTER_HOME'], str(home))
            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(key.read_text(), 'synthetic-saved-key' if usable else '')

    def test_cancelled_key_setup_does_not_continue_to_finish_setup_prompts(self):
        setup = module('native_setup')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = resources(root / 'download')
            home = root / 'home'
            home.mkdir()
            (home / 'current').write_text('1.2.0')
            dialog = mock.MagicMock()
            dialog.Progress.return_value.__enter__.return_value.cancelled = False
            owners = {name: mock.Mock() for name in ('desktop_menu', 'native_resources', 'atomic_write',
                                                    'native_controller', 'native_shortcuts')}
            owners['native_dialog'] = dialog
            owners['native_resources'].current.return_value = source
            original = setup.load
            with mock.patch.object(setup, 'load', side_effect=lambda name: owners.get(name) or original(name)), \
                    mock.patch.object(setup, 'retained', return_value=source), \
                    mock.patch.object(setup, 'run_installer', return_value=SimpleNamespace(returncode=0)), \
                    mock.patch.object(setup, '_key', return_value=2), \
                    mock.patch.object(setup, '_check') as check:
                self.assertEqual(setup.onboard(source, home, 'resume'), 0)
            check.assert_not_called()

    def test_handled_child_error_reaches_parent_without_a_second_message(self):
        setup = module('native_setup')
        messages = []
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / 'current').write_text('synthetic-release')
            state = dict(setup=dict(nonce='synthetic', expires=99999999999),
                         port=1, token='synthetic', clients='codex')
            owners = dict(native_dialog=SimpleNamespace(message=messages.append),
                diagnostics=SimpleNamespace(_provider_defs=lambda: SimpleNamespace(unhandled=lambda *a: True),
                                            _key_private=None),
                codex_config=SimpleNamespace(read_receipt=lambda *a: {'clients': {}}))
            def child(*args, **kwargs):
                with mock.patch.object(work_account, 'collect', side_effect=work_account.SignInFailed('Contact IT.')), \
                        mock.patch.object(key_prompt.key_dialog, 'message',
                                          side_effect=lambda tool, text, **kw: messages.append(text)):
                    code = key_prompt.run_window('http://127.0.0.1:1/t/setup/n',
                        tool_path='/dialog', platform_name='win32', work=True)
                return SimpleNamespace(returncode=code)
            with mock.patch.object(setup, 'load', side_effect=lambda name: owners[name]), \
                    mock.patch.object(setup, 'metadata', return_value={'runtime_exec': 'synthetic-python'}), \
                    mock.patch.object(setup, 'environment', return_value={}), \
                    mock.patch.object(setup, 'read_json', return_value=state), \
                    mock.patch.object(setup, 'onboard', side_effect=lambda *a: setup._key(home, home, run=child)):
                self.assertEqual(setup.main(['--resources', str(home), '--home', str(home)]), 1)
        self.assertEqual(messages, ['Contact IT.'])
