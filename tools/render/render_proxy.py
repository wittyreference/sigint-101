# ABOUTME: The render proxy: a loopback front for the console that the render checks' browser uses.
# ABOUTME: Forwards GET and HEAD of pages; refuses /api/* (403) and every other method (405).
import http.client
import posixpath
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

# The browser of a render check never gets the console's own address, only this proxy's. So no
# page, however long it stays loaded and whatever happens to DevTools, can reach the console's
# API or send it a write: the fence does not depend on the browser at all.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "proxy-connection"}
WRITES = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE")


def is_api(target):
    """Whether a request target names the console's API, however it is spelled.

    The path is percent-decoded and normalised the way the console's router would see it, so
    /%61pi/opened and /static/../api/opened are the API too.
    """
    raw = target.split("?", 1)[0].split("#", 1)[0]
    path = posixpath.normpath("/" + unquote(raw).lstrip("/"))
    return path == "/api" or path.startswith("/api/")


def make_handler(upstream):
    """A request handler that forwards to `upstream` (http://host:port) what a page may read."""
    parts = urlsplit(upstream)
    host, port = parts.hostname, parts.port or 80
    origin = f"{parts.scheme}://{parts.netloc}"

    class RenderProxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def _refuse(self, status, reason, allow=False):
            body = f"render proxy: {reason}\n".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if allow:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _forward(self):
            if not self.path.startswith("/"):
                self._refuse(400, "only paths on the console are served")
                return
            if is_api(self.path):
                self._refuse(403, "the console's API is closed to render checks")
                return
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in HOP_BY_HOP and key.lower() != "host"}
            try:
                conn = http.client.HTTPConnection(host, port, timeout=30)
                conn.request(self.command, self.path, headers=headers)
                reply = conn.getresponse()
            except OSError as error:
                self._refuse(502, f"the console did not answer ({error})")
                return
            try:
                self.send_response(reply.status, reply.reason)
                proxy = f"http://{self.headers.get('Host', '')}"
                for key, value in reply.getheaders():
                    if key.lower() in HOP_BY_HOP:
                        continue
                    if key.lower() == "location" and value.startswith(origin):
                        value = proxy + value[len(origin):]
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := reply.read(65536):
                        self.wfile.write(chunk)
            finally:
                conn.close()

        def do_GET(self):
            self._forward()

        def do_HEAD(self):
            self._forward()

    def refuse_write(self):
        self._refuse(405, f"{self.command} is closed to render checks; pages are read only",
                     allow=True)

    for method in WRITES:
        setattr(RenderProxy, f"do_{method}", refuse_write)
    return RenderProxy


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def serve_in_thread(upstream):
    """Start a proxy for `upstream` on an ephemeral loopback port in this process.

    For a tool run on its own: the proxy lives exactly as long as the tool, so once the tool
    exits a page it left loaded reaches nothing. Returns the proxy's address.
    """
    server = _Server(("127.0.0.1", 0), make_handler(upstream))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def main(fd, upstream):
    """Serve on a loopback socket the render check bound and handed over (its own process)."""
    listener = socket.socket(fileno=int(fd))
    server = _Server(listener.getsockname(), make_handler(upstream), bind_and_activate=False)
    server.socket.close()
    server.socket = listener
    server.serve_forever()


if __name__ == "__main__":
    main(*sys.argv[1:3])
