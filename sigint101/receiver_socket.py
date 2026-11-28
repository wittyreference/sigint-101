# ABOUTME: Common bind and occupied-port reporting for local receiver feeds.
# ABOUTME: A caller chooses whether to match a tool's address reuse policy.

import errno
import socket


class PortOccupied(RuntimeError):
    """Another socket prevents the receiver from binding its configured endpoint."""


def bind_receiver(address, socket_type, *, reuse=False, occupied=None):
    sock = socket.socket(socket.AF_INET, socket_type)
    try:
        if reuse:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
    except OSError as exc:
        try:
            sock.close()
        except OSError:
            pass
        if occupied is not None and exc.errno == errno.EADDRINUSE:
            protocol = "TCP" if socket_type == socket.SOCK_STREAM else "UDP"
            host, port = address
            raise occupied(f"{protocol} port {host}:{port} is already in use.") from exc
        raise
    return sock
