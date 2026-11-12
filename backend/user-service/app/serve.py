"""The API's entry point in the image: python -m app.serve.

One uvicorn process listens on PUBLIC_PORT and, when INTERNAL_PORT is set, on that port too.
The app tells them apart by the port each request arrived on (app/internal_port.py)."""

import socket

import uvicorn

from app.config import PUBLIC_PORT, Settings, get_settings

__all__ = ["PUBLIC_PORT", "listen_ports", "main"]


def listen_ports(settings: Settings) -> list[int]:
    if settings.internal_port is None:
        return [PUBLIC_PORT]
    return [PUBLIC_PORT, settings.internal_port]


def _listen(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # All interfaces: in a container, reachable only as its security group allows.
    sock.bind(("0.0.0.0", port))  # nosec B104
    return sock


def main() -> None:
    sockets = [_listen(port) for port in listen_ports(get_settings())]
    server = uvicorn.Server(uvicorn.Config("app.main:app", access_log=False))
    server.run(sockets=sockets)


if __name__ == "__main__":
    main()
