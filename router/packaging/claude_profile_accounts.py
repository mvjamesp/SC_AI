"""Read-only account-change plans and exclusive ownership for clean profiles.

The switch owner holds the installation, switch and both profile locks before
claiming a profile. A claim is not a task-sharing binding: Claude must create
its own gateway identity before the existing pair guard can allow sharing.
"""
import contextlib
import importlib.util
import json
import os
import re
from pathlib import Path


OWNER = '.claude-profile-control-owner.json'
ACCOUNTS = '.claude-profile-control-accounts.json'
DIGEST = re.compile('[0-9a-f]{64}')
OWNER_STAGE = re.compile(re.escape(OWNER) + r'\.tmp-[0-9a-f]{32}\Z')


def _identity():
    spec = importlib.util.spec_from_file_location(
        'router_account_identity', Path(__file__).with_name('claude_profile_identity.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rotation():
    spec = importlib.util.spec_from_file_location(
        'router_account_rotation', Path(__file__).with_name('claude_profile_rotation.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pending(home):
    return os.path.lexists(Path(home) / '.claude-profile-rotation-pending.json')


def policy_root(home, receipts, third):
    """Local policy follows the verified original while retention is pending."""
    return (_rotation().recovery_root(home, receipts) or third) if pending(home) else third


@contextlib.contextmanager
def _profile_locks(sync, roots):
    with contextlib.ExitStack() as locks:
        for root in sorted(roots):
            if not locks.enter_context(sync._service.install_lock(root, '.claude-profile-sync.lock')):
                raise ValueError('Claude is already sharing tasks. Try again shortly.')
        yield


def _read(path, identity):
    try:
        return identity._read_object(path)
    except FileNotFoundError:
        return None


def _owner(path, identity):
    value = _read(path, identity)
    if value is not None and (set(value) != {'version', 'account'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['account'], str) or not DIGEST.fullmatch(value['account'])):
        raise ValueError('Claude profile ownership could not be checked. Run SCG Router setup again.')
    return value


def _metadata(home, third, identity):
    saved = _read(Path(home) / ACCOUNTS, identity)
    owner = _owner(third / OWNER, identity)
    if saved is not None and (set(saved) != {'accounts'}
            or not isinstance(saved['accounts'], str) or not DIGEST.fullmatch(saved['accounts'])):
        raise ValueError('Claude profile ownership could not be checked. Run SCG Router setup again.')
    return saved, owner


def plan(home, context, destination, *, subscription):
    """Return a stable, read-only recovery decision before asking to restart.

    Unknown existing histories are not first-use setup. A clean destination can
    be initialized without task sharing; an old destination requires retention
    and rotation, never reassignment of the old sharing binding.
    """
    if not subscription:
        return {'rotate': False, 'return_only': False, 'native': None, 'gateway': None,
                'binding': None, 'owner': None, 'history': False}
    sync, _, (first, third) = context
    identity = sync._records._identity
    try:
        saved, owner = _metadata(home, third, identity)
    except (OSError, ValueError, TypeError, KeyError):
        if destination != '1p':
            raise
        return {'rotate': False, 'return_only': True, 'native': None, 'gateway': None,
                'binding': None, 'owner': None, 'history': False}
    native = identity.subscription(first) if subscription else None
    gateway = identity.third_party(third)
    current = identity.native_digest(first, native[0]) if native else None
    # Cowork is not shared, but its presence still rules out a new empty profile.
    history = any(sync._records.record_paths(third / store, include_irregular=True)
                  for store in ('claude-code-sessions', 'local-agent-mode-sessions'))
    used = any(path.name not in {'configLibrary', 'claude_desktop_config.json', OWNER,
                                '.claude-profile-sync.lock'} and not OWNER_STAGE.fullmatch(path.name)
               for path in third.iterdir())
    previous = bool(saved or owner or gateway or history or used)
    if destination == '1p' and previous and not native:
        return {'rotate': False, 'return_only': True, 'native': None,
                'gateway': gateway, 'binding': saved, 'owner': owner, 'history': history}
    if subscription and not native and (previous or destination == '3p'):
        raise ValueError('Open a Code task in Claude, then try again.')
    changed = bool(owner and current and owner['account'] != current)
    changed |= bool(saved and native and (not gateway
                              or saved['accounts'] != identity.pair_digest(first, native, third, gateway)))
    changed |= bool((history or used or gateway) and not saved and not owner)
    if changed and not native and subscription:
        raise ValueError('Open a Code task in Claude, then try again.')
    if not subscription:
        changed = False  # OpenRouter-only use preserves its existing profile and never shares it.
    return {'rotate': changed and destination == '3p',
            'return_only': changed and destination == '1p',
            'native': current, 'gateway': gateway,
            'binding': saved, 'owner': owner, 'history': history}


def claim(profile, account):
    """Pin a newly prepared profile before first launch, without rebinding tasks."""
    if not isinstance(account, str) or not DIGEST.fullmatch(account):
        raise ValueError('Claude profile ownership could not be checked.')
    profile = Path(profile)
    if any(path.is_symlink() for path in (profile, *profile.parents)):
        raise ValueError('Claude profile ownership could not be checked.')
    identity, path = _identity(), profile / OWNER
    value = {'version': 1, 'account': account}
    existing = _owner(path, identity)
    if existing is not None:
        if existing != value:
            raise ValueError('Claude profile ownership changed. Try switching again.')
        return
    _rotation()._exclusive(path, (json.dumps(value) + '\n').encode('utf-8'))
    if _owner(path, identity) != value:
        raise ValueError('Claude profile ownership changed. Try switching again.')


def can_reopen(home, context, attached, subscription):
    """A refused switch must not reopen a gateway from another account."""
    try:
        _, current = attached(home)
        if current['deploymentMode'] == '1p':
            return True
        account = plan(home, context, '3p', subscription=subscription)
        return not account['rotate'] and (not subscription or bool(account['owner'] or account['binding']))
    except (OSError, ValueError, TypeError, KeyError, SystemExit):
        return False


def claim_current(home, context, expected, check):
    """Pin first use only after the same account is rechecked under both locks."""
    sync, _, roots = context
    with _profile_locks(sync, roots):
        if plan(home, context, '3p', subscription=True) != expected:
            raise ValueError('Claude account changed. Try switching again.')
        check()
        claim(roots[1], expected['native'])
        latest = plan(home, context, '3p', subscription=True)
        if latest['native'] != expected['native'] or latest['rotate']:
            raise ValueError('Claude account changed. Try switching again.')
        return latest


def rotate(home, context, receipts, consent, check):
    """Retain the earlier account pair under the existing closed-app locks."""
    sync, _, roots = context
    with _profile_locks(sync, roots):
        if plan(home, context, '3p', subscription=True) != consent:
            raise ValueError('Claude account changed. Try switching again.')
        check()
        state = sync._records.read_json(Path(home) / 'mixed-router-state.json')
        rotation = _rotation()
        try:
            rotation.rotate(home, roots[1], state, receipts, check=check)
        except rotation.RotationError as exc:
            raise ValueError('Claude profile recovery could not finish safely. Try again or contact IT.') from exc
        claim(roots[1], consent['native'])
        latest = plan(home, context, '3p', subscription=True)
        if latest['native'] != consent['native'] or latest['rotate']:
            raise ValueError('Claude account changed. Try switching again.')
        check()


def recover(home, context, receipts, check, *, before_finish=lambda: None):
    """Recovery never launches Claude; conflicts retain the pending marker."""
    sync, _, roots = context
    rotation = _rotation()
    old = rotation.recovery_root(home, receipts)
    if old is None:
        return 'none'
    with _profile_locks(sync, (roots[0], old)):
        check()
        return rotation.recover(home, receipts, check=check, before_finish=before_finish)


def recovery(home, receipts, platform_ops, platform_name, run, context_for, attached, policy=None):
    """A retry recovers captured data before it may open either profile."""
    entry = receipts.read_receipt(home)['clients']['claude-desktop']
    third = Path(entry['config_path']).parents[1]
    context = context_for(third)
    policy = policy or (lambda: platform_ops.policy_disabled(
        platform_name, run=run, profile=policy_root(home, receipts, third)))
    if policy() is not False:
        raise ValueError('managed')
    if not platform_ops.confirm('Recover the previous Claude profile and return to your Claude '
            'subscription? Claude will restart. Your saved profiles and tasks will be kept.',
            platform_name, run=run):
        return None
    if platform_ops.request_stop(platform_name, run=run) is not True:
        raise ValueError('busy')
    def closed():
        if policy() is not False or platform_ops.is_running(platform_name, run=run) is not False:
            raise ValueError('busy')
    def select_subscription():
        config, _ = attached(home)
        before = config.read_bytes() if config.exists() else None
        current = _rotation()._json.document(before.decode('utf-8') if before is not None else '{}')
        if 'deploymentMode' in current:
            current.pop('deploymentMode')  # Missing selection is Claude's subscription default.
            closed()
            _rotation()._desktop.write_json(config, current, expected=before)
        if config.exists():
            with config.open('r+b') as stream:
                os.fsync(stream.fileno())
        _rotation()._publish._fsync_dir(config.parent)
        if attached(home)[1]['deploymentMode'] != '1p':
            raise ValueError('Claude selection changed. Try recovery again.')
    recover(home, context, receipts, closed, before_finish=select_subscription)
    answer = platform_ops.open_app(platform_name, run=run)
    if getattr(answer, 'returncode', 0) != 0:
        raise OSError('Claude did not open')
    return '1p'
