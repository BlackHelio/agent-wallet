#!/usr/bin/env python3
"""Run a CLI command under a real pty, feeding lines when prompts appear."""
import os, pty, select, sys, time

lines = [l for l in os.environ.get("FEED", "").split("\\n") if l != ""]
cmd = sys.argv[1:]
env = dict(os.environ)
pid, fd = pty.fork()
if pid == 0:
    os.execvpe(cmd[0], cmd, env)

buf = b""
sent = 0
deadline = time.time() + 40
while time.time() < deadline:
    r, _, _ = select.select([fd], [], [], 0.5)
    if r:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        # answer any prompt that ends the buffer with ': ' or '[y/N] '
        text = buf.decode(errors="replace")
        if sent < len(lines) and (text.rstrip().endswith(":") or text.rstrip().endswith("[y/N]")):
            os.write(fd, (lines[sent] + "\n").encode())
            sent += 1
            buf = b""
_, status = os.waitpid(pid, 0)
print(text[-2000:])
print(f"__rc={os.waitstatus_to_exitcode(status)}")
