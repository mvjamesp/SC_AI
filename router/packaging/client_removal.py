"""Preflight every attachment, then detach with retryable, receipt-owned edits.

No cross-client rollback: completed restores remain complete. A later failure
keeps the service running and leaves each remaining attachment's receipt.
"""
import hashlib
import importlib.util
import os
from pathlib import Path


def _load(name):
    spec = importlib.util.spec_from_file_location('router_remove_' + name,
                                                Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_json = _load('owned_json')
_atomic = _load('atomic_write')
LABELS = {'codex': 'Codex', 'claude': 'Claude Code', 'claude-desktop': 'Claude desktop'}


def read(path):
    try:
        with Path(path).open(newline='') as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def entry_path(entry):
    path = entry.get('config_path') if isinstance(entry, dict) else None
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise _json.Conflict('damaged_receipt')
    return Path(path)


def _snapshot(entry, keys, legacy_keys=()):
    previous = _json.snapshot(entry.get('inserted_block'), ())
    if legacy_keys and 'written' not in entry and set(previous) == set(legacy_keys):
        return previous
    return _json.snapshot(entry.get('inserted_block'), keys)


def saved_values(entry, keys, legacy_keys=()):
    try:
        return _snapshot(entry, keys, legacy_keys)
    except _json.Conflict:
        raise SystemExit('Assistant connection conflict: damaged_receipt.') from None


def _preserved_entry(home, path, before):
    """Retain opaque bytes without claiming knowledge of their plaintext."""
    privacy = _load('service').load('router_removal_privacy',
        Path(__file__).resolve().parents[1] / 'ai_router' / 'winacl.py')
    permissions = _load('claude_profile_permissions')
    permissions.require_private_parent(Path(home))
    if os.name == 'nt':
        privacy.require_owner_only(Path(home))
    raw = path.read_bytes()
    _atomic.check_unchanged(path, before)
    archive = Path(home) / ('claude-desktop-before-' + hashlib.sha256(raw).hexdigest() + '.json')
    if archive.is_symlink():
        raise _json.Conflict('connection_changed')
    old = archive.read_bytes() if archive.exists() else None
    if old is not None:
        privacy.require_owner_only(archive)
        permissions.require_private_parent(archive, directory=False)
        if old != raw:
            raise _json.Conflict('connection_changed')
    return archive, old, raw


def json_plan(home, client, entry, receipts=None):
    """The same narrow proof serves standalone detach and all-client removal."""
    if os.path.lexists(Path(home) / '.claude-profile-rotation-pending.json'):
        raise _json.Conflict('profile_recovery_pending')
    path = entry_path(entry)
    owner = _load('claude_config' if client == 'claude' else 'claude_desktop_config')
    previous = _snapshot(entry, owner.OWNED_KEYS, getattr(owner, 'LEGACY_KEYS', ()))
    written = entry.get('written')
    if written is None:  # Legacy receipts retain the connection in router state.
        state = _json.document((Path(home) / 'mixed-router-state.json').read_text())
        if not isinstance(state.get('port'), int) or not isinstance(state.get('token'), str):
            raise _json.Conflict('damaged_receipt')
        written = (owner.desired_env(home, state) if client == 'claude'
                   else owner.desired_values(state))
    base = set(owner.OWNED_KEYS)
    if (not isinstance(written, dict)
            or not base <= set(written) <= base | set(getattr(owner, 'NONE_KEYS', ()))
            or any(not isinstance(value, str) for value in written.values())):
        raise _json.Conflict('damaged_receipt')
    if any(value is not None and not isinstance(value, str) for value in previous.values()):
        raise _json.Conflict('damaged_receipt')
    if 'written' not in entry:
        written = {key: value for key, value in written.items() if key in previous}
    if client == 'claude' and owner.FIRST_PARTY_KEY in previous:
        written = dict(written, **{owner.FIRST_PARTY_KEY: '1'})
    before, guards, archive = read(path), {}, None
    if (client == 'claude-desktop' and receipts is not None and before is not None
            and owner.sealed(seal := _json.document(before).get(owner.API_KEY_KEY))):
        # The route proof cannot identify sealed plaintext. Keep the exact
        # entry before restoring the connection, so no opaque value is lost.
        state = _json.document(read(Path(home) / 'mixed-router-state.json') or '{}')
        try:
            owner.operational_snapshot(home, state, entry, receipts, observed_bytes=guards)
        except ValueError as exc:  # the owner raises its own module's Conflict class
            raise _json.Conflict(str(exc)) from None
        _atomic.check_unchanged(path, before)
        archive = _preserved_entry(home, path, before)
        written = dict(written, **{owner.API_KEY_KEY: seal})
    after = _json.restore(before if before is not None else '{}',
                          ('env',) if client == 'claude' else (), written, previous)
    config = path.parents[1] / 'claude_desktop_config.json' if client == 'claude-desktop' else None
    files = [archive] if archive is not None else []
    files += [(target, text, text) for target, text in guards.items() if target not in (path, config)]
    files.append((path, before, after if before is not None else None))
    if client == 'claude-desktop':
        if path.parent.name != 'configLibrary' or 'deploymentMode' not in previous:
            raise _json.Conflict('damaged_receipt')
        # Keep this selection guard on retries too, after the seal is already restored.
        meta = path.parent / '_meta.json'
        selected = meta.read_bytes() if meta.exists() else None
        library = _json.document(selected.decode('utf-8') if selected is not None else '{}')
        if library.get('appliedId') != path.stem or library.get('hybridPointer'):
            raise _json.Conflict('connection_changed')
        if meta not in guards:
            files.insert(0, (meta, selected, selected))
        old = read(config)
        if config in guards:
            _atomic.check_unchanged(config, guards[config])
        if old is not None:
            doc = _json.document(old)
            # A selection made after attachment belongs to the person.
            updated = (_json.restore(old, (), {'deploymentMode': '3p'}, previous)
                       if doc.get('deploymentMode') == '3p' else old)
            files.append((config, old, updated))
        elif config in guards:
            files.append((config, None, None))
    return files


def apply(files):
    # Read-only proof inputs and every write target must still match before
    # publication. Recheck proof inputs after writes before dropping a receipt.
    for path, before, _ in files:
        _atomic.check_unchanged(path, before)
    retained = [(path, after) for path, _, after in files if isinstance(after, bytes)]
    for path, before, after in files:
        target = path.resolve()
        for saved, raw in retained:
            if saved != path:
                _atomic.check_unchanged(saved, raw)
        if before != after:
            if isinstance(after, bytes):
                # Publish only complete private bytes, without replacing a competing copy.
                _load('claude_profile_permissions').require_private_parent(path.parent)
                _load('claude_profile_rotation')._exclusive(path, after)
            else:
                _atomic.replace_text(target, after, newline='', expected=before)
        else:
            _atomic.check_unchanged(target, before)
        actual = path.read_bytes() if isinstance(after, bytes) else read(path)
        if path.resolve() != target or actual != after:
            raise _atomic.ConcurrentChange('Settings changed concurrently; retry removal.')
    for path, _, after in files:
        _atomic.check_unchanged(path, after)


def detach_json(home, client, receipts, entry=None):
    current = receipts.read_receipt(home).get('clients', {}).get(client)
    if current is None:
        return
    if entry is not None and entry != current:
        raise _json.Conflict('receipt_changed')
    apply(json_plan(home, client, current, receipts))
    receipts.drop_client(home, client, expected=current)


def preflight(home, receipts):
    if os.path.lexists(Path(home) / '.claude-profile-rotation-pending.json'):
        raise SystemExit('Choose Claude in SCG Router to recover the saved profile before removal.')
    path = receipts.receipt_path_for(home)
    raw = read(path)
    if raw is not None:
        doc = _json.document(raw)
        if path.is_symlink() or not (doc.get('version') == 2 and isinstance(doc.get('clients'), dict)
                or isinstance(doc.get('config_path'), str) and isinstance(doc.get('inserted_block'), str)):
            raise _json.Conflict('damaged_receipt')
    entries = receipts.read_receipt(home).get('clients', {})
    if set(entries) - LABELS.keys():
        raise _json.Conflict('unsupported_attachment')
    _load('codex_switch_config').refuse_switched(home)
    _load('claude_profile_hook').removal_plan(home)
    plans, paths = [], set()
    for client, entry in entries.items():
        try:
            if client == 'codex':
                before, after = receipts.removal_plan(entry_path(entry), entry)
                files = [(entry_path(entry), before, after)]
            else:
                files = json_plan(home, client, entry, receipts)
            targets = {path.resolve() for path, _, _ in files}
            if paths & targets:
                raise _json.Conflict('ambiguous_settings_path')
            paths.update(targets)
            plans.append((client, entry, files))
        except (OSError, ValueError, SystemExit) as exc:
            code = (str(exc) if isinstance(exc, _json.Conflict) else exc.reason
                    if isinstance(exc, receipts._owned.OwnershipConflict) else 'ownership_conflict')
            raise SystemExit('%s removal conflict: %s. No assistants were detached.'
                             % (LABELS[client], code)) from None
    return plans


def remove_all(home, receipts, restore_codex):
    """All ownership checks precede every detach; service lifetime stays outside."""
    plans = preflight(home, receipts)
    removed = []
    for client, entry, files in plans:
        try:
            if receipts.read_receipt(home).get('clients', {}).get(client) != entry:
                raise _json.Conflict('receipt_changed')
            if client == 'codex':
                before = files[0][1]
                restore_codex(hashlib.sha256(before.encode()).hexdigest(), entry)
            else:
                apply(files)
                receipts.drop_client(home, client, expected=entry)
            removed.append(LABELS[client])
        except (OSError, ValueError, SystemExit):
            remaining = [LABELS[name] for name, _, _ in plans if LABELS[name] not in removed]
            raise SystemExit('Removal incomplete. Removed: %s. Remaining: %s. '
                             'Router services left running; retry removal.'
                             % (', '.join(removed) or 'none', ', '.join(remaining))) from None
    return removed
