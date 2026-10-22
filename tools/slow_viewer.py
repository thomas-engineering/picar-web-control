#!/usr/bin/env python3
"""Check that the video byte budget shrinks for a viewer that reads too slowly.

    python tools/slow_viewer.py picar.local:8000

Opens /video, reads about 60 KiB/s for 12 s and prints each viewer's budget from /api/status.
Over a real network the budget should fall from 150-300 KiB/s towards the 25 KiB/s floor; on
localhost the kernel buffers hide the slow reader and it will not. The list should be empty
after the viewer disconnects.
"""
import json
import socket
import sys
import time
import urllib.request

host = sys.argv[1]
name, _, port = host.partition(":")
status = lambda: json.load(urllib.request.urlopen(f"http://{host}/api/status", timeout=5))

sock = socket.create_connection((name, int(port or 80)))
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
sock.sendall(b"GET /video HTTP/1.1\r\nHost: x\r\n\r\n")
start = time.time()
received = 0
next_report = 3
while time.time() - start < 12:
    received += len(sock.recv(2048))
    time.sleep(0.03)  # about 60 KiB/s, below what 640x480 produces
    if time.time() - start >= next_report:
        print(f"{time.time() - start:4.1f} s: read {received / 1024:.0f} KiB, video {status()['video']}")
        next_report += 3
sock.close()
time.sleep(3)
print("after disconnect:", status()["video"], "viewers", status()["viewers"])
