"""Cancellable console input without an executor thread waiting in input()."""

from __future__ import annotations

import asyncio
import codecs
import os
import sys


class TerminalInput:
    """Windows console polling or POSIX readiness, owned by one UI instance."""

    def __init__(self, *, stdin=None, stdout=None):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self._queue = asyncio.Queue()
        self._fd = None
        self._old_blocking = None
        self._buffer = ""
        self._decoder = None
        self._reading = False
        self._closed = False
        self._eof = False

    async def __aenter__(self):
        if os.name == "nt":
            if not self.stdin.isatty():
                raise ValueError("P2 CLI requires a controlling Windows terminal")
        else:
            try:
                self._fd = self.stdin.fileno()
                self._old_blocking = os.get_blocking(self._fd)
                self._decoder = codecs.getincrementaldecoder(self.stdin.encoding or "utf-8")()
                os.set_blocking(self._fd, False)
                asyncio.get_running_loop().add_reader(self._fd, self._ready)
            except (AttributeError, OSError, ValueError, NotImplementedError) as exc:
                await self.aclose()
                raise ValueError("P2 CLI requires a terminal/pipe supported by async readiness") \
                    from exc
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    def _ready(self):
        try:
            chunk = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            self._queue.put_nowait(exc)
            self._eof = True
            asyncio.get_running_loop().remove_reader(self._fd)
            return
        if not chunk:
            self._eof = True
            asyncio.get_running_loop().remove_reader(self._fd)
            try:
                self._buffer += self._decoder.decode(b"", final=True)
            except UnicodeError as exc:
                self._queue.put_nowait(exc)
                return
            if self._buffer:
                self._queue.put_nowait(self._buffer)
                self._buffer = ""
            self._queue.put_nowait(EOFError("console input closed"))
            return
        try:
            self._buffer += self._decoder.decode(chunk)
        except UnicodeError as exc:
            self._queue.put_nowait(exc)
            self._eof = True
            asyncio.get_running_loop().remove_reader(self._fd)
            return
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._queue.put_nowait(line.rstrip("\r"))

    async def __call__(self, prompt: str) -> str:
        if self._closed or self._reading:
            raise RuntimeError("terminal input is closed or already in use")
        self._reading = True
        self.stdout.write(prompt)
        self.stdout.flush()
        try:
            if os.name == "nt":
                return await self._windows_line()
            if self._fd is None:
                raise RuntimeError("enter TerminalInput before reading")
            if self._eof and self._queue.empty():
                raise EOFError("console input closed")
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self._reading = False

    async def _windows_line(self):
        import msvcrt

        chars = []
        extended_key = False
        while True:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if extended_key:
                    extended_key = False
                    continue
                if char in {"\x00", "\xe0"}:
                    # Extended keys have a second code, not answer text.
                    extended_key = True
                    continue
                if char == "\x03":
                    raise asyncio.CancelledError
                if char == "\x1a":
                    raise EOFError("console input closed")
                if char in {"\r", "\n"}:
                    self.stdout.write("\n")
                    self.stdout.flush()
                    return "".join(chars)
                if char == "\b":
                    if chars:
                        chars.pop()
                        self.stdout.write("\b \b")
                else:
                    chars.append(char)
                    self.stdout.write(char)
                self.stdout.flush()
            await asyncio.sleep(0.02)

    async def aclose(self):
        self._closed = True
        if self._fd is not None:
            asyncio.get_running_loop().remove_reader(self._fd)
            if self._old_blocking is not None:
                os.set_blocking(self._fd, self._old_blocking)
            self._fd = None
