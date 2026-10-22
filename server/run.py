"""Start the web server on one socket that accepts both IPv4 and IPv6.

uvicorn's --host :: makes asyncio set IPV6_V6ONLY, which drops IPv4, so the socket is opened here.
Run: python -m server.run [port]
"""
import socket
import sys

import uvicorn


def dual_stack_socket(port):
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        address = ("::", port)
    except OSError:  # no IPv6 on this machine
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        address = ("0.0.0.0", port)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Connections accepted from this socket inherit it: the kernel then holds back at most this
    # much unsent data, so old video frames cannot pile up in front of the control messages.
    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_NOTSENT_LOWAT", 25), 16384)
    sock.bind(address)
    return sock


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    config = uvicorn.Config("server.app:app", timeout_graceful_shutdown=3)
    uvicorn.Server(config).run(sockets=[dual_stack_socket(port)])


if __name__ == "__main__":
    main()
