"""Retain Claude profiles without transferring identity; never start or bind Claude.
Callers obtain consent, stop Claude, hold installer/switch/profile locks, block
pending profiles and lock the retained original on recovery. Recheck accounts before 3p.
"""
import importlib.util
import json
import os
import re
import stat
import uuid
from pathlib import Path
def _load(name):
    spec = importlib.util.spec_from_file_location(
        'router_rotation_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
_desktop = _load('claude_desktop_config')
_publish = _load('claude_profile_publish')
_atomic = _desktop.atomic_write
_json = _desktop._removal._json
_permissions = _load('claude_profile_permissions')
_status = _load('update_status')
io_path = _publish.io_path
PENDING = '.claude-profile-rotation-pending.json'
ACCOUNTS = '.claude-profile-control-accounts.json'
LEDGER = '.claude-profile-control-ledger.json'
STATE = 'mixed-router-state.json'
UUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')

class RotationError(ValueError):
    """A content-free refusal. A pending journal must be recovered first."""
def _absolute(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or path == path.parent):
        raise RotationError('unsafe-path')
    _publish._records.assert_ancestors(path)
    return path
def _directory(path):
    path = _absolute(path)
    info = io_path(path).lstat()
    if (not stat.S_ISDIR(info.st_mode) or
            (os.name != 'nt' and info.st_uid != os.getuid())):
        raise RotationError('unsafe-directory')
    return path
def _parent(path):
    _directory(path)
    _permissions.require_private_parent(io_path(path))
    if os.name == 'nt':
        _publish._winacl.require_owner_only(io_path(path), windows=True)
    elif io_path(path).stat().st_mode & 0o022:
        raise RotationError('unsafe-profile-parent')
def _bytes(path, *, optional=False):
    path = _absolute(path)
    try:
        info = io_path(path).lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise RotationError('missing-file') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            (os.name != 'nt' and info.st_uid != os.getuid())):
        raise RotationError('unsafe-file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(io_path(path)), flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RotationError('changed-file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read()
            after = os.fstat(stream.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    again = io_path(path).lstat()
    stamp = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if stamp(info) != stamp(after) or stamp(after) != stamp(again):
        raise RotationError('changed-file')
    return raw
def _object(raw):
    try:
        return _json.document(raw.decode('utf-8'))
    except (UnicodeError, ValueError, TypeError):
        raise RotationError('invalid-document') from None
def _exclusive(path, raw):
    _absolute(path)
    staged = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    fd = os.open(str(io_path(staged)), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _publish.move_new(staged, path)
    _publish._fsync_dir(path.parent)
def _root_identity(path):
    info = io_path(_directory(path)).stat()
    return _raw_json({'device': info.st_dev, 'inode': info.st_ino})
def _marker(home, raw):
    _exclusive(home / PENDING, raw)
def _remove_marker(home, expected, check=lambda: None):
    """Hide no pending work: remove only the exact original marker generation."""
    marker = home / PENDING
    _same(marker, expected)
    _publish._fsync_dir(home)
    check()
    # Final fallible operation: unlink commits; no failure is reported after it.
    _atomic.remove(marker, expected=expected)
def _finish(home, archive, name, marker, check=lambda: None):
    target = archive / name
    if _exists(target):
        _same(target, marker)
    else:
        _exclusive(target, marker)
    _remove_marker(home, marker, check)
def _raw_json(doc):
    return (json.dumps(doc, indent=2) + '\n').encode('utf-8')
def _same(path, expected):
    if _bytes(path, optional=True) != expected:
        raise RotationError('changed-state')
def _recovery_state(home, saved):
    if _status.configuration(_object(_bytes(home / STATE))) != _status.configuration(_object(saved)):
        raise RotationError('changed-state')
def _tree(path):
    _directory(path)
    def fail(exc):
        raise exc
    for parent, dirs, files in os.walk(io_path(path), followlinks=False, onerror=fail):
        for item in dirs + files:
            info = (Path(parent) / item).lstat()
            if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or
                    getattr(info, 'st_file_attributes', 0) & 0x400 or
                    (os.name != 'nt' and info.st_uid != os.getuid())):
                raise RotationError('unsafe-profile')
def _profile_from_receipt(raw):
    clients = _object(raw).get('clients')
    if not isinstance(clients, dict) or not isinstance(clients.get(_desktop.CLIENT_KEY), dict):
        raise RotationError('invalid-receipt')
    value = clients[_desktop.CLIENT_KEY].get('config_path')
    if not isinstance(value, str):
        raise RotationError('invalid-receipt')
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or path.parent.name != 'configLibrary'
            or not UUID.fullmatch(path.stem) or path.name != path.stem + '.json'):
        raise RotationError('invalid-receipt')
    return path.parent.parent
def _receipt_path(home, receipts):
    path = _absolute(receipts.receipt_path_for(home))
    if path.parent != home or path.name != receipts.RECEIPT_NAME:
        raise RotationError('unsafe-receipt')
    return path
def _exists(path):
    return os.path.lexists(io_path(path))
def pending(home):
    """Whether rotation needs recovery; a malformed or redirected marker is pending."""
    return _exists(_absolute(home) / PENDING)
def _private_dir(path):
    _parent(path.parent)
    io_path(path).mkdir(mode=0o700)
    if os.name == 'nt':
        _publish._winacl.require_owner_only(io_path(path), windows=True)
    _publish._fsync_dir(path.parent)

def _preflight(home, profile, state, receipts):
    home, profile = _directory(home), _directory(profile)
    _parent(home)
    _parent(profile.parent)
    if home == profile or home in profile.parents or profile in home.parents:
        raise RotationError('overlapping-roots')
    if pending(home):
        raise RotationError('rotation-pending')
    receipt_path = _receipt_path(home, receipts)
    receipt_raw = _bytes(receipt_path)
    receipt = _object(receipt_raw)
    state_raw = _bytes(home / STATE)
    if not isinstance(state, dict) or _object(state_raw) != state:
        raise RotationError('changed-state')
    if receipt.get('version') != 2 or _profile_from_receipt(receipt_raw) != profile:
        raise RotationError('invalid-receipt')
    entry = receipt['clients'][_desktop.CLIENT_KEY]
    _tree(profile)
    observed = {}
    try:
        _desktop.operational_snapshot(home, state, entry, receipts, observed_bytes=observed)
    except (OSError, ValueError, TypeError, KeyError, SystemExit):
        raise RotationError('attachment-changed') from None
    controls = {name: _bytes(home / name, optional=True) for name in (ACCOUNTS, LEDGER)}
    if controls[ACCOUNTS] is not None:
        binding = _object(controls[ACCOUNTS]).get('accounts')
        if not isinstance(binding, str) or not re.fullmatch('[0-9a-f]{64}', binding):
            raise RotationError('invalid-binding')
    return receipt_path, receipt_raw, state_raw, observed, controls
def _paths(profile, ident):
    prefix = '.' + profile.name + '.rotation-' + ident
    return (profile.parent / (prefix + '.archive'),
            profile.parent / (prefix + '.stage'))
def _load_journal(home):
    raw = _bytes(home / PENDING)
    doc = _object(raw)
    if (set(doc) != {'version', 'profile', 'id'} or doc['version'] != 1
            or not isinstance(doc['id'], str) or not UUID.fullmatch(doc['id'])):
        raise RotationError('invalid-journal')
    profile = _absolute(doc['profile'])
    _parent(profile.parent)
    if home == profile or home in profile.parents or profile in home.parents:
        raise RotationError('invalid-journal')
    return raw, profile, doc['id'], *_paths(profile, doc['id'])
def recovery_root(home, receipts):
    """Find the old root to lock. Never create a lock in the clean replacement."""
    home = _directory(home)
    if not pending(home):
        return None
    _, profile, _, archive, _ = _load_journal(home)
    if _profile_from_receipt(_bytes(_receipt_path(home, receipts))) != profile:
        raise RotationError('invalid-journal')
    if _exists(archive):
        _directory(archive)
    root = archive / 'profile' if _exists(archive / 'profile') else profile
    _directory(root)
    if _exists(archive / 'original-root.json'):
        _same(archive / 'original-root.json', _root_identity(root))
    return root
def _stage(stage, profile, ident, state):
    _private_dir(stage)
    library = stage / 'configLibrary'
    _private_dir(library)
    entry = profile / 'configLibrary' / (ident + '.json')
    _exclusive(library / '_meta.json', _raw_json({
        'appliedId': ident, 'entries': [{'id': ident, 'name': 'SCG Router'}]}))
    _exclusive(library / (ident + '.json'), _raw_json(_desktop.desired_values(state)))
    _exclusive(stage / 'claude_desktop_config.json', _raw_json({}))
    _publish._fsync_dir(stage)
    return entry
def _after_receipt(before, ident, profile):
    text = before.decode('utf-8')
    doc, start = _json._scope(text, ('clients',))
    if not isinstance(doc, dict) or _desktop.CLIENT_KEY not in doc:
        raise RotationError('invalid-receipt')
    member = next(row for row in _json._members(text, start)[0]
                  if row[0] == _desktop.CLIENT_KEY)
    entry = dict(doc[_desktop.CLIENT_KEY])
    entry['config_path'] = str(profile / 'configLibrary' / (ident + '.json'))
    entry['inserted_block'] = json.dumps(
        {key: None for key in _desktop.OWNED_KEYS + ('deploymentMode',)}, sort_keys=True)
    return _json._edit(text, [(member[2], member[3], json.dumps(entry))]).encode('utf-8')
def _clean_profile(profile, ident, state_raw):
    _tree(profile)
    library = profile / 'configLibrary'
    if {p.name for p in io_path(profile).iterdir()} != {'configLibrary', 'claude_desktop_config.json'}:
        raise RotationError('new-profile-changed')
    if {p.name for p in io_path(library).iterdir()} != {'_meta.json', ident + '.json'}:
        raise RotationError('new-profile-changed')
    if (_object(_bytes(library / '_meta.json')) != {
            'appliedId': ident, 'entries': [{'id': ident, 'name': 'SCG Router'}]}
            or _object(_bytes(library / (ident + '.json'))) !=
            _desktop.desired_values(_object(state_raw))
            or _object(_bytes(profile / 'claude_desktop_config.json')) != {}):
        raise RotationError('new-profile-changed')
def rotate(home, profile, state, receipts, *, check=lambda: None):
    """Return a clean same-root entry after caller-authorized retention.
    Recheck stopped-app status at publication; post-journal errors stay pending.
    """
    home, profile = _absolute(home), _absolute(profile)
    receipt_path, before, state_raw, observed, controls = _preflight(home, profile, state, receipts)
    ident = str(uuid.uuid4())
    archive, stage = _paths(profile, ident)
    if any(_exists(item) for item in (archive, stage)):
        raise RotationError('archive-exists')
    marker = _raw_json({'version': 1, 'profile': str(profile), 'id': ident})
    _marker(home, marker)
    _private_dir(archive)
    after = _after_receipt(before, ident, profile)
    images = {STATE: state_raw, receipt_path.name: before, 'receipt.after.json': after,
              'controls.json': _raw_json({name: raw is not None for name, raw in controls.items()})}
    images.update({name: raw for name, raw in controls.items() if raw is not None})
    for name, raw in images.items():
        _exclusive(archive / name, raw)
    _exclusive(archive / 'original-root.json', _root_identity(profile))
    result = _stage(stage, profile, ident, state)
    checks = {receipt_path: before, home / STATE: state_raw}
    checks.update({home / name: raw for name, raw in controls.items()})
    checks = {**observed, **checks}  # The original receipt and whole-state bytes stay authoritative.
    for path, raw in checks.items():
        _same(path, raw)
    _clean_profile(stage, ident, state_raw)
    _same(home / PENDING, marker)
    _tree(profile)
    _same(archive / 'original-root.json', _root_identity(profile))
    check()
    _publish.move_new(profile, archive / 'profile')
    _same(archive / 'original-root.json', _root_identity(archive / 'profile'))
    check()
    _publish.move_new(stage, profile)
    _clean_profile(profile, ident, state_raw)
    for name, raw in controls.items():
        _same(home / name, raw)
        if raw is not None:
            _same(archive / name, raw)
            _atomic.remove(home / name, expected=raw)
    _same(home / STATE, state_raw)
    _same(receipt_path, before)
    check()
    _parent(home)
    receipts.replace_receipt(home, after, expected=before)
    _publish._fsync_dir(home)
    _same(receipt_path, after)
    _same(home / STATE, state_raw)
    _clean_profile(profile, ident, state_raw)
    for name in controls:
        _same(home / name, None)
    _same(home / PENDING, marker)
    _finish(home, archive, 'rotation.json', marker, check)
    return result
def recover(home, receipts, *, check=lambda: None, before_finish=lambda: None):
    """Return 'none' or 'restored'; hold rotate()'s locks and stopped-app checks.
    Finalize caller selection before clearing pending; conflicts retain all evidence.
    """
    home = _directory(home)
    if not pending(home):
        return 'none'
    _parent(home)
    def complete():
        check()
        before_finish()
        check()
    marker, profile, ident, archive, stage = _load_journal(home)
    if _profile_from_receipt(_bytes(_receipt_path(home, receipts))) != profile:
        raise RotationError('invalid-journal')
    if not _exists(archive):
        if _exists(stage):
            raise RotationError('recovery-conflict')
        _directory(profile)
        _remove_marker(home, marker, complete)
        return 'restored'
    _directory(archive)
    receipt_path = _receipt_path(home, receipts)
    saved_profile = archive / 'profile'
    if not _exists(saved_profile):
        _directory(profile)
        if not _exists(archive / 'restoring'):
            if _exists(archive / 'original-root.json'):
                _same(archive / 'original-root.json', _root_identity(profile))
            _remove_marker(home, marker, complete)
            return 'restored'
        _same(archive / 'original-root.json', _root_identity(profile))
    before = _bytes(archive / receipt_path.name)
    state_raw = _bytes(archive / STATE)
    after = _bytes(archive / 'receipt.after.json')
    if after != _after_receipt(before, ident, profile) or _profile_from_receipt(before) != profile:
        raise RotationError('invalid-journal')
    _recovery_state(home, state_raw)
    current = _bytes(receipt_path)
    if current not in (before, after):
        raise RotationError('recovery-conflict')
    presence = _object(_bytes(archive / 'controls.json'))
    if set(presence) != {ACCOUNTS, LEDGER} or any(type(x) is not bool for x in presence.values()):
        raise RotationError('invalid-journal')
    controls = {}
    for name in (ACCOUNTS, LEDGER):
        saved, live = (_bytes(path, optional=True) for path in (archive / name, home / name))
        if presence[name] != (saved is not None) or live not in (None, saved):
            raise RotationError('recovery-conflict')
        controls[name] = (saved, live)
    if _exists(saved_profile):
        _tree(saved_profile)
        profile_exists, stage_exists = _exists(profile), _exists(stage)
        if profile_exists == stage_exists:
            raise RotationError('recovery-conflict')
        _clean_profile(profile if profile_exists else stage, ident, state_raw)
        _same(archive / 'original-root.json', _root_identity(saved_profile))
        if _exists(archive / 'restoring'):
            _same(archive / 'restoring', b'')
        else:
            _exclusive(archive / 'restoring', b'')
        # Retain the clean generation instead of deleting it during rollback.
        if profile_exists:
            check()
            _publish.move_new(profile, stage)
        check()
        _publish.move_new(saved_profile, profile)
    else:
        _tree(profile)
        _clean_profile(stage, ident, state_raw)
        _same(archive / 'original-root.json', _root_identity(profile))
    for name, (saved, live) in controls.items():
        if saved is not None and live is None:
            _exclusive(home / name, saved)
    if current == after:
        check()
        _parent(home)
        receipts.replace_receipt(home, before, expected=after)
        _publish._fsync_dir(home)
    _same(receipt_path, before)
    _recovery_state(home, state_raw)
    for name, (saved, _) in controls.items():
        _same(home / name, saved)
    _same(home / PENDING, marker)
    _finish(home, archive, 'recovered.json', marker, complete)
    return 'restored'
