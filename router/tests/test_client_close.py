"""Windows early refusals survive split writes; cleanup stays bounded."""
import http.client
import json
import os
import socket
import threading
import unittest
from select import select
from types import SimpleNamespace
from unittest import mock

from ai_router import client_close, router, transport
from tests.harness import StubCredentials


class CloseBoundsTests(unittest.TestCase):
    def test_continuously_readable_peer_cannot_extend_total_deadline(self):
        sock = mock.Mock()
        sock.recv.return_value = b'x'
        with mock.patch.object(client_close, 'monotonic',
                               side_effect=[10, 10.05, 10.20, 10.26]), \
                mock.patch.object(client_close, 'select',
                                  return_value=([sock], [], [])) as wait:
            client_close.half_close_and_drain(sock)
        sock.shutdown.assert_called_once_with(socket.SHUT_WR)
        self.assertEqual(sock.recv.call_count, 2)
        self.assertEqual(wait.call_count, 2)
        self.assertAlmostEqual(wait.call_args_list[0].args[3], 0.20)
        self.assertAlmostEqual(wait.call_args_list[1].args[3], 0.05)

    def test_drain_stops_at_byte_cap_in_bounded_reads(self):
        sock = mock.Mock()
        sock.recv.side_effect = lambda size: b'x' * size
        with mock.patch.object(client_close, 'monotonic', return_value=10), \
                mock.patch.object(client_close, 'select',
                                  return_value=([sock], [], [])):
            client_close.half_close_and_drain(sock)
        reads = [call.args[0] for call in sock.recv.call_args_list]
        self.assertEqual(sum(reads), 64 * 1024)
        self.assertEqual(max(reads), 16 * 1024)

    def test_idle_peer_waits_once_without_reading(self):
        sock = mock.Mock()
        with mock.patch.object(client_close, 'monotonic', return_value=10), \
                mock.patch.object(client_close, 'select',
                                  return_value=([], [], [])) as wait:
            client_close.half_close_and_drain(sock)
        wait.assert_called_once_with([sock], [], [], 0.25)
        sock.recv.assert_not_called()

    def test_peer_eof_ends_drain(self):
        sock = mock.Mock()
        sock.recv.return_value = b''
        with mock.patch.object(client_close, 'select',
                               return_value=([sock], [], [])):
            client_close.half_close_and_drain(sock)
        sock.recv.assert_called_once()

    def test_windows_always_closes_after_transport_failures(self):
        for phase in ('shutdown', 'select', 'recv'):
            with self.subTest(phase=phase):
                server = router.Router.__new__(router.Router)
                server.close_request = mock.Mock()
                sock = mock.Mock()
                if phase != 'select':
                    getattr(sock, phase).side_effect = OSError('peer disconnected')
                with mock.patch.object(router, 'os', SimpleNamespace(name='nt')), \
                        mock.patch.object(client_close, 'select',
                                          return_value=([sock], [], []),
                                          side_effect=OSError('peer disconnected')
                                          if phase == 'select' else None):
                    server.shutdown_request(sock)
                server.close_request.assert_called_once_with(sock)

    def test_windows_closes_even_after_an_unexpected_failure(self):
        server = router.Router.__new__(router.Router)
        server.close_request = mock.Mock()
        sock = mock.Mock()
        with mock.patch.object(router, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(client_close, 'half_close_and_drain',
                                  side_effect=ValueError('unexpected')):
            with self.assertRaises(ValueError):
                server.shutdown_request(sock)
        server.close_request.assert_called_once_with(sock)

    def test_posix_delegates_to_standard_library(self):
        server = router.Router.__new__(router.Router)
        sock = mock.Mock()
        with mock.patch.object(router, 'os', SimpleNamespace(name='posix')), \
                mock.patch.object(router.ThreadingHTTPServer, 'shutdown_request') as close, \
                mock.patch.object(client_close, 'half_close_and_drain') as drain:
            server.shutdown_request(sock)
        close.assert_called_once_with(sock)
        drain.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Windows late-write socket regression')
class WindowsLateBodyTests(unittest.TestCase):
    def test_late_split_body_keeps_complete_early_refusals(self):
        for guard, status, code in (
                ('draining', 503, 'SERVER_DRAINING'),
                ('forbidden', 403, 'FORBIDDEN'),
                ('transfer_encoding', 400, 'INVALID_REQUEST_SIZE'),
                ('ceiling', 400, 'BODY_TOO_LARGE')):
            with self.subTest(guard=guard):
                self._late_body(guard, status, code)

    def _late_body(self, guard, status, code):
        ready = threading.Event()

        class ObservedRouter(router.Router):
            def close_request(self, request):
                super().close_request(request)
                # With the old immediate close this releases the body only
                # after closing, deterministically reproducing WinError 10053.
                ready.set()

        def closing_wait(read, write, error, timeout):
            # With graceful close the body arrives while the server waits;
            # actual socket readiness and the production deadline stay intact.
            ready.set()
            return select(read, write, error, timeout)

        server = ObservedRouter({'port': 0, 'token': 'test-token', 'openai_models': []},
                                credentials=StubCredentials())
        server.draining = guard == 'draining'
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        payload = b'{"model":"x"}'
        try:
            with mock.patch.object(client_close, 'select', side_effect=closing_wait), \
                    mock.patch.object(router, 'log_event'), \
                    mock.patch.object(transport, 'MAX_REQUEST_BYTES',
                                      1 if guard == 'ceiling' else transport.MAX_REQUEST_BYTES):
                worker.start()
                path = '/nope/responses' if guard == 'forbidden' else '/test-token/responses'
                conn.putrequest('POST', path)
                conn.putheader('Content-Type', 'application/json')
                conn.putheader('Content-Length', str(len(payload)))
                if guard == 'transfer_encoding':
                    conn.putheader('Transfer-Encoding', 'chunked')
                conn.endheaders()
                self.assertTrue(ready.wait(3), 'server never reached response cleanup')
                conn.send(payload)
                with conn.getresponse() as response:
                    self.assertEqual(response.status, status)
                    self.assertEqual(json.loads(response.read())['error']['code'], code)
                    if guard == 'draining':
                        self.assertEqual(response.getheader('Retry-After'), '2')
        finally:
            conn.close()
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
