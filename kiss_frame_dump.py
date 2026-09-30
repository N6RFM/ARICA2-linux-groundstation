#!/usr/bin/env python3
"""
Connects to Direwolf's KISS TCP port and prints the complete raw bytes of
every frame it receives, in hex, one per line, with the KISS FEND framing
included. Use this to capture exact frame content for figuring out
ARICA-2's actual downlink address format - much more reliable than reading
partial bytes off a terminal screen.

Usage:
    python3 kiss_frame_dump.py [host] [port]

Defaults to 127.0.0.1:8001 (Direwolf).
"""

import socket
import sys
import time

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8001

sock = socket.create_connection((host, port), timeout=5)
sock.settimeout(1.0)
print(f"Connected to {host}:{port}. Dumping full KISS frames (Ctrl+C to stop)...\n")

buf = b""
try:
    while True:
        try:
            data = sock.recv(1024)
        except socket.timeout:
            continue
        if not data:
            print("Connection closed by remote end.")
            break
        buf += data
        while True:
            start = buf.find(b"\xc0")
            if start == -1:
                break
            end = buf.find(b"\xc0", start + 1)
            if end == -1:
                break
            frame = buf[start:end + 1]
            buf = buf[end + 1:]
            ts = time.strftime("%H:%M:%S")
            hexstr = " ".join(f"{b:02x}" for b in frame)
            print(f"[{ts}] ({len(frame)} bytes) {hexstr}")
except KeyboardInterrupt:
    print("\nStopped.")
finally:
    sock.close()
