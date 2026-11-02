"""Offer one honest Codex restart action after a verified setup change."""
import hashlib
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_payload import load


def target(home):
    """Return the in-memory settings/catalogue target; never persist its digest."""
    try:
        clients = load('codex_config').read_receipt(home).get('clients', {})
        entry = clients.get('codex') if isinstance(clients, dict) else None
        if not isinstance(entry, dict):
            return None
        block = entry.get('inserted_block')
        if not isinstance(block, str) or not block:
            return None
        path = Path(home) / 'mixed-models.json'
        catalogue = path.read_bytes() if path.is_file() else b''
        return block, hashlib.sha256(catalogue).hexdigest()
    except (OSError, ValueError, TypeError):
        return None


def offer(home, before, codex_was_running):
    """Handle changed/open Codex completion once, even if its dialog cannot finish."""
    after = target(home)
    if sys.platform != 'win32' or after is None or before == after:
        return False
    notice = load('notice')
    still_running = codex_was_running
    if still_running:
        platform = load('claude_desktop_platform')
        app = load('codex_desktop_switch').CODEX
        try:
            still_running = platform.is_running('win32', app=app)
        except (OSError, subprocess.SubprocessError):
            still_running = False
    if not still_running:
        notice.suppress_closed_client_restart(home)
        return False
    try:
        notice.record_client_change(home, load('service').current_version(home), uuid.uuid4().hex)
        load('notice_windows').show(home, setup_completion=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    return True
