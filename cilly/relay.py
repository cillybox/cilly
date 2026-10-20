"""stdin/stdout <-> Unix socket relay.

QEMU's ``guestfwd=tcp:10.0.2.100:3128-cmd:...`` spawns this once per guest TCP
connection.  It is deliberately standalone (stdlib only, no package imports)
so it can run under ``python3 -I`` inside the bwrap sandbox.

Usage: python3 -I relay.py /path/to/proxy.sock
"""

import os
import selectors
import socket
import sys


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sys.argv[1])
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    sel = selectors.DefaultSelector()
    sel.register(stdin, selectors.EVENT_READ, "in")
    sel.register(s, selectors.EVENT_READ, "sock")
    live = 2
    while live:
        for key, _ in sel.select():
            if key.data == "in":
                data = os.read(stdin, 65536)
                if not data:
                    sel.unregister(stdin)
                    live -= 1
                    s.shutdown(socket.SHUT_WR)
                else:
                    s.sendall(data)
            else:
                data = s.recv(65536)
                if not data:
                    return 0
                view = memoryview(data)
                while view:
                    n = os.write(stdout, view)
                    view = view[n:]
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, ConnectionError):
        sys.exit(0)
