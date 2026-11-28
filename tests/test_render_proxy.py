# ABOUTME: Tests the render proxy (tools/render/render_proxy.py) that fronts the console for checks.
# ABOUTME: Pages pass as GET and HEAD; the API and every write are refused before the console.

import http.client
import importlib.util
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RENDER = REPO / "tools" / "render"


def _module(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", RENDER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def console():
    """A stand-in console that records each request and can answer with a redirect."""
    hits = []

    class Console(BaseHTTPRequestHandler):
        def _answer(self):
            hits.append((self.command, self.path))
            port = self.server.server_port
            redirects = {
                "/operate": (303, f"http://127.0.0.1:{port}/operate/waterfall"),
                # The same console, spelled another way: case and an explicit default-less port.
                "/mixed/": (307, f"HTTP://LocalHost:{port}/mixed"),
                "/relative": (302, "/lesson/1?x=1"),
                # Elsewhere: another port, another host, another scheme, a sneaky userinfo.
                "/elsewhere-port": (302, "http://127.0.0.1:9/lesson/1"),
                "/elsewhere-host": (302, "http://example.invalid/lesson/1"),
                "/elsewhere-scheme": (302, f"https://127.0.0.1:{port}/lesson/1"),
                "/elsewhere-protocol-relative": (302, "//example.invalid/x"),
            }
            if self.path in redirects:
                status, location = redirects[self.path]
                self.send_response(status)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"<title>SIGINT 101</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _answer

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Console) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", hits
        finally:
            server.shutdown()


def _request(proxy, method, path):
    host, port = proxy.split("//", 1)[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request(method, path, body=b"x" if method in ("POST", "PUT", "PATCH") else None)
        reply = conn.getresponse()
        return reply.status, reply.headers, reply.read()   # headers: case-insensitive names
    finally:
        conn.close()


def test_pages_pass_through_as_get_and_head(console):
    upstream, hits = console
    proxy = _module("render_proxy").serve_in_thread(upstream)
    status, _, body = _request(proxy, "GET", "/lesson/1?recall=done")
    assert (status, body) == (200, b"<title>SIGINT 101</title>")
    assert _request(proxy, "HEAD", "/")[0] == 200
    assert hits == [("GET", "/lesson/1?recall=done"), ("HEAD", "/")]


def test_a_redirect_stays_on_the_proxy(console):
    upstream, _ = console
    proxy = _module("render_proxy").serve_in_thread(upstream)
    status, headers, _ = _request(proxy, "GET", "/operate")
    assert status == 303
    assert headers["Location"] == f"{proxy}/operate/waterfall"


@pytest.mark.parametrize("path", ["/api/opened?lesson_id=1", "/api/decode", "/api", "/%61pi/stage",
                                  "/static/../api/complete", "//api/rack/place", "/api/../api/x"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_the_api_is_refused_however_it_is_spelled(console, path, method):
    upstream, hits = console
    proxy = _module("render_proxy").serve_in_thread(upstream)
    status, _, body = _request(proxy, method, path)
    assert status == 403
    if method == "GET":
        assert b"API is closed" in body
    assert hits == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/lesson/1", "/api/opened?lesson_id=1"])
def test_every_write_is_refused_with_405(console, method, path):
    upstream, hits = console
    proxy = _module("render_proxy").serve_in_thread(upstream)
    status, headers, body = _request(proxy, method, path)
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert b"read only" in body
    assert hits == []


@pytest.mark.parametrize("path", ["/API", "/Api/decode", "/aPi/opened?lesson_id=1", "/%41PI/stage",
                                  "/static/../API/complete", "//ApI/rack/place"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_the_api_is_refused_in_any_case(console, path, method):
    upstream, hits = console
    proxy = _module("render_proxy").serve_in_thread(upstream)
    assert _request(proxy, method, path)[0] == 403
    assert hits == []


@pytest.mark.parametrize("method", ["PROPFIND", "MKCOL", "LOCK", "SEARCH", "BREW", "TRACE",
                                    "CONNECT"])
def test_any_other_method_gets_405_with_allow(console, method):
    upstream, hits = console
    proxy = _module("render_proxy").serve_in_thread(upstream)
    status, headers, _ = _request(proxy, method, "/lesson/1")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert hits == []


@pytest.mark.parametrize("path, kept", [("/mixed/", "/mixed"), ("/relative", "/lesson/1?x=1")])
def test_a_redirect_to_the_same_console_is_kept_on_the_proxy(console, path, kept):
    # The upstream is addressed in mixed case, as the reviewer's LOCALHOST was; the console's
    # redirect names it in another case. Either way the browser stays on the proxy.
    upstream, hits = console
    port = upstream.rsplit(":", 1)[1]
    proxy = _module("render_proxy").serve_in_thread(f"http://LOCALHOST:{port}")
    status, headers, _ = _request(proxy, "GET", path)
    assert 300 <= status < 400
    assert headers["Location"] == f"{proxy}{kept}"
    assert hits == [("GET", path)]


@pytest.mark.parametrize("path", ["/elsewhere-port", "/elsewhere-host", "/elsewhere-scheme",
                                  "/elsewhere-protocol-relative"])
def test_a_redirect_away_from_the_console_is_refused(console, path):
    upstream, _ = console
    proxy = _module("render_proxy").serve_in_thread(upstream)
    status, headers, body = _request(proxy, "GET", path)
    assert status == 502
    assert "Location" not in headers
    assert b"redirect away from the console" in body


def test_the_stock_consoles_slash_redirect_stays_on_the_proxy(tmp_path):
    # The reviewer's case on the real console: a page asked for with a trailing slash answers
    # 307 to the same page without it, naming the console's host. With the console addressed in
    # mixed case, the redirect must still land on the proxy, and following it must reach the
    # page there; the console must see nothing but those GETs.
    render_check = _module("render_check")
    with render_check.scratch_console(tmp_path / "console") as url:
        port = url.rsplit(":", 1)[1]
        before = (tmp_path / "console" / "events.jsonl").read_bytes() if (
            tmp_path / "console" / "events.jsonl").exists() else b""
        proxy = _module("render_proxy").serve_in_thread(f"http://LOCALHOST:{port}")
        status, headers, _ = _request(proxy, "GET", "/lesson/29/")
        assert status == 307, (status, headers)
        assert headers["Location"].startswith(proxy + "/lesson/29"), headers["Location"]
        landed = headers["Location"][len(proxy):]
        assert _request(proxy, "GET", landed)[0] == 200
        assert _request(proxy, "POST", "/api/opened?lesson_id=29")[0] == 405
        assert _request(proxy, "GET", "/API/decode")[0] == 403
        after = (tmp_path / "console" / "events.jsonl").read_bytes() if (
            tmp_path / "console" / "events.jsonl").exists() else b""
        assert after == before


def _listening(port):
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in Path(table).read_text().splitlines()[1:]:
            local, state = row.split()[1], row.split()[3]
            address, hexport = local.rsplit(":", 1)
            if int(hexport, 16) == port and state == "0A":
                found.append(address)
    return found


def _running(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def test_the_runners_proxy_is_its_own_loopback_process_and_stops_with_the_run(console, capsys):
    upstream, hits = console
    render_check = _module("render_check")
    alive = []
    with render_check.render_proxy(upstream, alive) as proxy:
        out = capsys.readouterr().out
        pid = int(out.rsplit("(process ", 1)[1].split(")")[0])
        assert pid != os.getpid() and _running(pid)
        port = int(proxy.rsplit(":", 1)[1])
        assert _listening(port) == ["0100007F"]
        assert _request(proxy, "POST", "/api/opened?lesson_id=1")[0] == 405
        assert alive, "the proxy counts as running from launch"
    assert not _running(pid)
    assert alive == []
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert [hit for hit in hits if hit[0] != "HEAD"] == []
