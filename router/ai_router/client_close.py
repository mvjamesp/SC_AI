"""Bounded transport cleanup for Windows clients still sending a refused body."""
import socket
from select import select
from time import monotonic

CLOSE_WAIT_S = 0.25
CLOSE_DRAIN_BYTES = 64 * 1024
CLOSE_READ_BYTES = 16 * 1024


def half_close_and_drain(sock):
    """Keep the sent response readable while a late request body arrives."""
    try:
        sock.shutdown(socket.SHUT_WR)
        deadline = monotonic() + CLOSE_WAIT_S
        remaining = CLOSE_DRAIN_BYTES
        # Closing before a split-write client's body arrives loses the response
        # on Windows. Discard transport bytes only, never trust or parse framing.
        # ponytail: late senders can outlive this grace; revisit the bounds when
        # measured client behavior requires a longer or larger closing window.
        while remaining:
            wait = deadline - monotonic()
            if wait <= 0 or not select([sock], [], [], wait)[0]:
                break
            chunk = sock.recv(min(CLOSE_READ_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        pass  # Disconnected clients still reach the caller's final close.
