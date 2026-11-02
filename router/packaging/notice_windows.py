"""Windows restart notices: presentation and verified restart are separate facts."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_payload import load, metadata

FAILED = ('Codex could not restart automatically. Finish active work, then quit and reopen '
          'Codex yourself and start a new chat. Your key and saved chats are unchanged.')


def command(home):
    """Use retained code and Python: the updater's extraction is disposable."""
    resources = load('native_resources').current(home)
    doc = metadata(resources, 'windows/x86_64')
    worker = resources / 'router/packaging/notice_windows.py'
    if not worker.is_file():
        raise OSError('The restart notice is unavailable')
    return [str(resources / 'runtime' / doc['runtime_exec']), str(worker), '--home', str(home)]


def pending(home, setup_completion=False):
    path = Path(home) / 'update-notice.json'
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not isinstance(doc, dict) or not isinstance(doc.get('version'), str) or not doc['version']:
        return path, raw, None
    text = load('notice_text')
    if doc.get('completion_selected') and not setup_completion:
        return path, raw, None
    if doc.get('shown'):
        if doc.get('names'):
            text.acknowledge(home, doc['names'])
        return path, raw, None
    names = doc.get('names')
    if not isinstance(names, list):
        names = text.pending_names(home)
    doc['names'] = [name for name in names if isinstance(name, str) and name]
    return path, raw, doc if (doc['names'] or doc.get('attachment')
                              or doc.get('client_change')) else None


def offer(home, spawn):
    """A worker takes the dialog lock and records presentation only after an answer."""
    try:
        if pending(home)[2] is None:
            return False
        spawn(command(home))
        return True
    except (OSError, ValueError):
        return False


def save(atomic, path, doc, raw):
    updated = (json.dumps(doc, indent=2) + '\n').encode('utf-8')
    atomic.replace_bytes(path, updated, expected=raw)
    return updated


def show(home, setup_completion=False):
    if os.environ.get('ROUTER_NO_NOTICE') == '1':
        return False
    home = Path(home)
    with load('service').install_lock(home, '.update-notice.lock') as held:
        if not held:
            if setup_completion:
                try:
                    _, raw, notice = pending(home, setup_completion=setup_completion)
                    prior = json.loads(raw)
                    return bool(notice is not None or prior.get('shown') and
                                (prior.get('attachment') or prior.get('client_change')))
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            return False
        path, raw, doc = pending(home, setup_completion=setup_completion)
        if doc is None:
            if setup_completion:
                try:
                    prior = json.loads(raw)
                    return bool(prior.get('shown') and
                                (prior.get('attachment') or prior.get('client_change')))
                except (ValueError, TypeError, AttributeError):
                    pass
            return False
        text, dialog, atomic = load('notice_text'), load('windows_dialog'), load('atomic_write')
        clients = text.clients(doc.get('clients'))
        restart = clients in ('codex', 'both')
        has_restart_reason = bool(doc.get('attachment') or doc.get('client_change'))
        platform = None
        app = None
        identity = None
        if setup_completion:
            if not has_restart_reason or not restart:
                return False
            platform = load('claude_desktop_platform')
            app = platform.App('com.openai.codex', ())
            try:
                if not platform.is_running('win32', app=app):
                    return False
                identity = platform.app_identity('win32', app=app)
            except (OSError, subprocess.SubprocessError):
                return False
            message = ("We couldn't confirm Codex loaded the changes. You can restart Codex, then "
                       'choose Check setup.\n\nFinish any running work first. Restarting will '
                       'close and reopen Codex.')
        else:
            message = text.message(doc['version'], clients, doc['names'],
                                   has_restart_reason)
        if restart and not setup_completion:
            message += '\n\nFinish active work before restarting Codex.'
        answer = dialog.show(message, home, restart, labels=('Restart Codex', 'Later')
                             if restart else ('OK',))
        doc.update(shown=True, restart_pending=restart,
                   restart_result=('pending' if answer else 'later') if restart else 'not_required')
        if setup_completion:
            doc['freshness'] = 'unknown'
        try:
            raw = save(atomic, path, doc, raw)
        except atomic.ConcurrentChange:
            dialog.show('SCG Router changed while this notice was open. Finish active work, '
                        'then quit and reopen Codex yourself and choose Check setup in SCG Router.',
                        home, False, labels=('OK',))
            return True
        if doc['names']:
            text.acknowledge(home, doc['names'])
        if not restart or not answer:
            return True
        if platform is None:
            platform = load('claude_desktop_platform')
            app = platform.App('com.openai.codex', ())
        try:
            with load('native_dialog').Progress(
                    'Restarting Codex. This may take a minute.\n\n'
                    'OK only hides this message; the restart will continue.', button='OK', home=home):
                if setup_completion and not platform.is_running('win32', app=app):
                    doc.update(restart_pending=False, restart_result='not_open')
                    save(atomic, path, doc, raw)
                    return True
                identity = identity or platform.app_identity('win32', app=app)
                if not platform.request_stop('win32', app=app, identity=identity):
                    raise OSError('Codex is still running')
                platform.open_app('win32', app=app, identity=identity)
        except Exception as exc:
            doc['restart_result'] = 'failed'
            load('update_reconcile').log(home, 'restart_notice', 'failed', error=type(exc).__name__)
            try:
                save(atomic, path, doc, raw)
            finally:
                dialog.show(FAILED, home, False, labels=('OK',))
        else:
            doc.update(restart_pending=False, restart_result='done')
            save(atomic, path, doc, raw)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        show(args.home)
    except (OSError, ValueError, RuntimeError) as exc:
        load('update_reconcile').log(args.home, 'restart_notice', 'unconfirmed', error=type(exc).__name__)
    return 0


if __name__ == '__main__':
    sys.exit(main())
