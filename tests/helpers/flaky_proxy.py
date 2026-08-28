"""Programmable TCP proxy for simulating LB / network failure modes.

Sits between OdooConnection and a real Odoo instance, intercepts traffic, and
exposes hooks to inject the failure modes that are otherwise impossible to
reproduce locally:

- ``silently_drop_upstream()`` — close the upstream socket without notifying
  the client. Models the LB-silent-drop scenario from issue #68: the client's
  cached keepalive socket looks alive at the userspace level, but reads block
  forever (without a socket timeout) or until the OS RTO retransmit gives up.

- ``set_response_delay(seconds)`` — hold each chunk of upstream traffic for N
  seconds before forwarding. Models a slow Odoo backend; useful for verifying
  that legitimate slow operations succeed within the configured timeout.

The proxy is a plain in-process Python TCP forwarder — no toxiproxy or extra
container needed. Designed to live alongside the unit-style perf tests that
already exercise the transport layer in isolation.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class _Tunnel:
    """A single client↔proxy↔upstream forwarding pair."""

    client_sock: socket.socket
    upstream_sock: socket.socket
    drop_event: threading.Event = field(default_factory=threading.Event)
    # Delays apply ONCE per tunnel, before the first chunk in each direction
    # is forwarded — this matches the "Odoo sees the request 6s after it was
    # sent" / "client sees the response 6s after Odoo sent it" pattern from
    # issue #68 (a per-chunk delay would multiply with HTTP send() chunking).
    request_delay: float = 0.0
    response_delay: float = 0.0
    _request_delayed: bool = False
    _response_delayed: bool = False


class FlakyProxy:
    """In-process TCP proxy with programmable failure injection.

    Not thread-safe in the sense of "use one instance from many test threads"
    — but the proxy itself uses internal threads for accept/forward loops,
    which is fine. Tests should construct one proxy per test and stop it in
    teardown.
    """

    def __init__(self, upstream_host: str, upstream_port: int):
        self._upstream: Tuple[str, int] = (upstream_host, upstream_port)
        self._listen: Optional[socket.socket] = None
        self._port: Optional[int] = None
        self._stop = threading.Event()
        self._tunnels: List[_Tunnel] = []
        self._lock = threading.Lock()
        self._next_request_delay: float = 0.0
        self._next_response_delay: float = 0.0
        self._accept_thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("Proxy not started")
        return self._port

    def start(self) -> int:
        """Bind to a free port on 127.0.0.1 and start accepting. Returns the port."""
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(16)
        self._port = self._listen.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self._port

    def stop(self) -> None:
        """Stop accepting, close all tunnels."""
        self._stop.set()
        if self._listen is not None:
            try:
                self._listen.close()
            except OSError:
                pass
        with self._lock:
            for t in self._tunnels:
                for s in (t.client_sock, t.upstream_sock):
                    try:
                        s.close()
                    except OSError:
                        pass
            self._tunnels.clear()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)

    def silently_drop_upstream(self) -> None:
        """Close upstream sockets on all active tunnels WITHOUT notifying clients.

        After this call, future client→upstream traffic is silently discarded,
        and future upstream→client forwarding stops. The client socket stays
        open at the OS level — reads will block until the client's own socket
        timeout fires, exactly like an LB that has dropped its upstream
        keepalive without sending FIN/RST.
        """
        with self._lock:
            for t in self._tunnels:
                t.drop_event.set()
                try:
                    t.upstream_sock.close()
                except OSError:
                    pass

    def set_response_delay(self, seconds: float) -> None:
        """Apply this delay (once per tunnel, before the first forwarded
        chunk) to upstream→client forwarding on all FUTURE tunnels. Existing
        tunnels are unaffected — the delay is captured at accept time so a
        test can change behavior between connections.
        """
        self._next_response_delay = float(seconds)

    def set_request_delay(self, seconds: float) -> None:
        """Apply this delay (once per tunnel, before the first forwarded
        chunk) to client→upstream forwarding on all FUTURE tunnels. Mirror of
        ``set_response_delay`` but on the request path — useful for
        reproducing the issue #68 reporter's "6s before Odoo sees the
        request" pattern.
        """
        self._next_request_delay = float(seconds)

    def active_tunnel_count(self) -> int:
        with self._lock:
            return len(self._tunnels)

    def _accept_loop(self) -> None:
        assert self._listen is not None
        self._listen.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client_sock, _ = self._listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                upstream_sock = socket.create_connection(self._upstream, timeout=5.0)
            except OSError:
                try:
                    client_sock.close()
                except OSError:
                    pass
                continue
            tunnel = _Tunnel(
                client_sock=client_sock,
                upstream_sock=upstream_sock,
                request_delay=self._next_request_delay,
                response_delay=self._next_response_delay,
            )
            with self._lock:
                self._tunnels.append(tunnel)
            threading.Thread(target=self._tunnel_loop, args=(tunnel,), daemon=True).start()

    def _tunnel_loop(self, tunnel: _Tunnel) -> None:
        c2u = threading.Thread(target=self._forward_client_to_upstream, args=(tunnel,), daemon=True)
        u2c = threading.Thread(target=self._forward_upstream_to_client, args=(tunnel,), daemon=True)
        c2u.start()
        u2c.start()
        c2u.join()
        u2c.join()
        for s in (tunnel.client_sock, tunnel.upstream_sock):
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            if tunnel in self._tunnels:
                self._tunnels.remove(tunnel)

    def _forward_client_to_upstream(self, tunnel: _Tunnel) -> None:
        """Drain client→upstream. After drop, keep draining but discard, so the
        client can still write at TCP level (its send buffer stays drained)."""
        client = tunnel.client_sock
        upstream = tunnel.upstream_sock
        client.settimeout(0.1)
        while not self._stop.is_set():
            try:
                data = client.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            if tunnel.drop_event.is_set():
                continue  # discard — upstream is gone but client still writing
            if tunnel.request_delay > 0 and not tunnel._request_delayed:
                tunnel._request_delayed = True
                deadline = time.monotonic() + tunnel.request_delay
                while time.monotonic() < deadline:
                    if self._stop.is_set() or tunnel.drop_event.is_set():
                        return
                    time.sleep(max(0.0, min(0.05, deadline - time.monotonic())))
            try:
                upstream.sendall(data)
            except OSError:
                return

    def _forward_upstream_to_client(self, tunnel: _Tunnel) -> None:
        """Drain upstream→client, optionally delaying each chunk. Stops on drop."""
        client = tunnel.client_sock
        upstream = tunnel.upstream_sock
        upstream.settimeout(0.1)
        while not self._stop.is_set():
            if tunnel.drop_event.is_set():
                return
            try:
                data = upstream.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            if tunnel.response_delay > 0 and not tunnel._response_delayed:
                tunnel._response_delayed = True
                # Sleep in small increments so stop/drop can interrupt promptly.
                deadline = time.monotonic() + tunnel.response_delay
                while time.monotonic() < deadline:
                    if self._stop.is_set() or tunnel.drop_event.is_set():
                        return
                    time.sleep(max(0.0, min(0.05, deadline - time.monotonic())))
            try:
                client.sendall(data)
            except OSError:
                return
