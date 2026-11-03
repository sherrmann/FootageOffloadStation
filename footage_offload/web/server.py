"""HTTP server for the Flask app: werkzeug's threaded server on all interfaces.

The station is reached by IP address on whatever network it is in, so it
always listens on every interface: IPv4 on 0.0.0.0 and, where the system
supports it, IPv6 on :: (IPv6-only socket, so both can share the port).

The sockets are bound here and handed to werkzeug as file descriptors,
because werkzeug's own binding calls sys.exit() when the port is taken.
"""
import logging
import socket
import threading

from werkzeug.serving import make_server

log = logging.getLogger(__name__)


def _bind(family, host, port):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        sock.listen(64)
    except BaseException:
        sock.close()
        raise
    return sock


class WebServer:
    """One werkzeug server per address family, each in its own thread."""

    def __init__(self, app, port, ipv6=True):
        self.app = app
        self.port = port
        self.servers = []
        self._threads = []
        sock4 = _bind(socket.AF_INET, "0.0.0.0", port)   # must work; raises OSError otherwise
        self.port = sock4.getsockname()[1]                # the real port when 0 was asked for
        self._add("0.0.0.0", sock4)
        if ipv6 and socket.has_ipv6:
            try:
                sock6 = _bind(socket.AF_INET6, "::", self.port)
            except OSError as e:
                log.info("IPv6 not available (%s); serving IPv4 only", e)
            else:
                self._add("::", sock6)

    def _add(self, host, sock):
        server = make_server(host, self.port, self.app, threaded=True, fd=sock.fileno())
        # werkzeug duplicated the descriptor; our copy is not needed any more.
        sock.close()
        server.daemon_threads = True
        self.servers.append(server)

    def start(self):
        for server in self.servers:
            t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5},
                                 name=f"http-{server.host}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("web UI on port %d (%s)", self.port, ", ".join(s.host for s in self.servers))
        return self

    def wait(self):
        """Block until shutdown() is called from another thread."""
        for t in list(self._threads):
            t.join()

    def serve_forever(self):
        """Start (if needed) and block until shutdown()."""
        if not self._threads:
            self.start()
        self.wait()

    def shutdown(self, timeout=5.0):
        for server in self.servers:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("stopping %s", server.host)
        for t in self._threads:
            t.join(timeout)
        for server in self.servers:
            try:
                server.server_close()
            except OSError:
                pass


def serve(app, port, ipv6=True):
    """Start serving `app` on all interfaces in background threads.

    Returns the running WebServer; `wait()` blocks, `shutdown()` stops it."""
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return WebServer(app, port, ipv6=ipv6).start()
