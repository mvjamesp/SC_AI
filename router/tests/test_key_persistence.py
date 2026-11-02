"""Updates keep an existing service credential across client mode changes."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_router.credentials import Credentials
from tests._platform import unix_only
from tests.install_fixtures import _craft_bundle, _ok_run, _patch_attrs, _static_fetch, install_mod
from tests.test_install_config import _fake_write_catalogue


@unix_only
class InstalledKeyPersistenceTests(unittest.TestCase):
    def test_updates_and_client_modes_preserve_each_supported_saved_key_source(self):
        _patch_attrs(self, run=_ok_run, self_test=lambda *args: None,
                     write_catalogue=_fake_write_catalogue,
                     wait_version=lambda *args, **kwargs: True)
        for source in ('router-file', 'opencode'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                user_home, home = root / 'user', root / 'custom-router-home'
                home.mkdir()
                env = {'HOME': str(user_home), 'PATH': '/usr/bin:/bin',
                       'ROUTER_HOME': str(home)}
                state = install_mod.ensure_state(home)
                if source == 'router-file':
                    credential = home / 'openrouter.key'
                    content = 'synthetic-persistent-key\n'
                else:
                    credential = user_home / '.local/share/opencode/auth.json'
                    credential.parent.mkdir(parents=True)
                    content = json.dumps({'openrouter': {
                        'type': 'api', 'key': 'synthetic-persistent-key'}})
                    state['opencode_auth'] = str(credential)
                    (home / 'mixed-router-state.json').write_text(json.dumps(state))
                credential.write_text(content)
                credential.chmod(0o600)
                before = (credential.read_bytes(), credential.stat().st_mtime_ns)
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(install_mod, '_open_key_window') as window, \
                        contextlib.redirect_stdout(io.StringIO()):
                    for version, mode in (('1.0.0', 'subscription'), ('1.0.1', 'none'),
                                          ('1.0.2', 'subscription')):
                        blob, latest = _craft_bundle(version)
                        args = install_mod.build_parser().parse_args([
                            '--feed', 'file:///latest.json', '--home', str(home),
                            '--openai-mode', mode])
                        self.assertEqual(install_mod.do_install(
                            args, _fetch=_static_fetch(latest, blob)), 0)
                        saved = json.loads((home / 'mixed-router-state.json').read_text())
                        self.assertNotIn('setup', saved)
                        self.assertEqual(saved['openai_mode'], mode)
                        self.assertEqual(Credentials(saved, home, env=env).key_for(),
                                         'synthetic-persistent-key')
                        self.assertEqual((credential.read_bytes(), credential.stat().st_mtime_ns),
                                         before)
                        if source == 'opencode':
                            self.assertFalse((home / 'openrouter.key').exists())
                    window.assert_not_called()


if __name__ == '__main__':
    unittest.main()
