"""Watch Linux directory events as they occur, without saving trace output."""

import ctypes
import os
from pathlib import Path
import re
import select
import stat
import struct
import sys
from tempfile import NamedTemporaryFile


MODIFY = 0x00000002
CLOSE_WRITE = 0x00000008
MOVED_FROM = 0x00000040
MOVED_TO = 0x00000080
CREATE = 0x00000100
DELETE = 0x00000200
OVERFLOW = 0x00004000
IGNORED = 0x00008000
IS_DIR = 0x40000000
DIRECTORY_ONLY = 0x03000000  # IN_DONT_FOLLOW | IN_ONLYDIR
EVENT = struct.Struct("iIII")
CHANGE = MODIFY | CLOSE_WRITE | MOVED_TO | CREATE
LIBC = ctypes.CDLL(None, use_errno=True)


class DirectoryWatch:
    def __init__(self):
        self.fd = self.checked(LIBC.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))
        self.directories = {}

    @staticmethod
    def checked(result):
        if result == -1:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        return result

    def add(self, directory):
        try:
            watch = self.checked(
                LIBC.inotify_add_watch(
                    self.fd, os.fsencode(directory), CHANGE | MOVED_FROM | DELETE | DIRECTORY_ONLY,
                )
            )
        except FileNotFoundError:
            return
        self.directories[watch] = directory

    def tree(self, root):
        for name, _, files in os.walk(root, onerror=self.walk_error):
            directory = Path(name)
            self.add(directory)
            yield from (directory / file for file in files)

    @staticmethod
    def walk_error(error):
        if not isinstance(error, FileNotFoundError):
            raise error

    def read(self):
        while True:
            try:
                data = os.read(self.fd, 262_144)
            except BlockingIOError:
                return
            offset = 0
            while offset < len(data):
                watch, mask, _, length = EVENT.unpack_from(data, offset)
                offset += EVENT.size
                name = data[offset:offset + length].split(b"\0", 1)[0]
                offset += length
                if mask & OVERFLOW:
                    raise RuntimeError("Privacy audit file event queue overflowed")
                if mask & IGNORED:
                    self.directories.pop(watch, None)
                elif watch in self.directories and name:
                    yield self.directories[watch] / os.fsdecode(name), mask

    def close(self):
        os.close(self.fd)


class HostLogWatch:
    """Read only newly written bytes from explicitly selected text logs."""

    def __init__(self, root, marker):
        self.root = root
        self.containers = root / "containerd/root/com.docker.services.container.v1.local"
        self.audit = root.parent / "auditkit"
        self.marker = marker
        self.watch = DirectoryWatch()
        self.files = {}
        self.matches = set()
        self.seen_paths = set()
        self.armed = False
        for directory in (root, root / "crashes", root / "mcp", self.audit):
            if not directory.is_dir():
                raise RuntimeError(f"Missing Docker log directory {directory}")
            self.watch.add(directory)
            for path in directory.iterdir():
                self.open_log(path, existing=True)
        if not self.containers.is_dir():
            raise RuntimeError("Missing Docker container log directory")
        for path in self.watch.tree(self.containers):
            self.open_log(path, existing=True)

    def is_log(self, path):
        if path.parent == self.root:
            return re.fullmatch(r"(daemon|daemon-stderr|client|tui)\.log(?:\.\d+)?", path.name)
        if path.parent == self.root / "crashes":
            return path.name.endswith(".log")
        if path.parent == self.root / "mcp":
            return path.name == "mcp.log"
        if path.parent == self.audit:
            return path.name.startswith("audit-") and path.name.endswith(".jsonl")
        return (
            path.parent.parent == self.containers
            and re.fullmatch(r"[0-9a-f]{64}", path.parent.name)
            and re.fullmatch(rf"{path.parent.name}-json\.log(?:\.\d+)?", path.name)
        )

    def open_log(self, path, *, existing=False):
        if not self.is_log(path):
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if self.armed and not existing and path not in self.seen_paths:
                raise RuntimeError("A newly written host log disappeared before inspection") from None
            return
        metadata = os.fstat(fd)
        key = (metadata.st_dev, metadata.st_ino)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            return
        self.seen_paths.add(path)
        if key in self.files:
            os.close(fd)
            return
        if existing:
            os.lseek(fd, 0, os.SEEK_END)
        self.files[key] = (fd, path, b"")

    def check(self):
        for path, mask in self.watch.read():
            if mask & IS_DIR and mask & (CREATE | MOVED_TO) and path.is_relative_to(self.containers):
                for child in self.watch.tree(path):
                    self.open_log(child)
            elif mask & CHANGE:
                self.open_log(path)
        for key, (fd, path, tail) in self.files.items():
            if os.fstat(fd).st_size < os.lseek(fd, 0, os.SEEK_CUR):
                os.lseek(fd, 0, os.SEEK_SET)
                tail = b""
            while data := os.read(fd, 65_536):
                contents = tail + data
                if self.marker in contents:
                    self.matches.add(path)
                tail = contents[-(len(self.marker) - 1):]
            self.files[key] = (fd, path, tail)
        if self.matches:
            paths = ", ".join(str(path.relative_to(self.root.parent)) for path in sorted(self.matches))
            raise RuntimeError(f"Synthetic conversation marker found in host logs ({paths})")

    def start_marker_window(self):
        self.check()
        self.armed = True

    def close(self):
        for fd, _, _ in self.files.values():
            os.close(fd)
        self.watch.close()


def guest_watch():
    workspace = Path.cwd()
    artifact = workspace / "privacy-audit-artifact.txt"
    # Guest /tmp and the private agent directory are writable state roots; both
    # disappear with the VM.
    roots = (
        Path("/home/agent"), Path("/tmp"), Path("/var/tmp"), Path("/var/log"), workspace,
    )
    watch = DirectoryWatch()
    try:
        initial = set()
        for root in roots:
            if not root.is_dir():
                raise RuntimeError(f"Missing guest audit root {root}")
            initial.update(watch.tree(root))
        with NamedTemporaryFile(dir="/tmp", prefix="pi-audit-control-") as control:
            control.write(b"File monitor self-check\n")
            control.flush()
        seen = 0
        for path, mask in watch.read():
            if path == Path(control.name):
                seen |= mask
        if seen & (CREATE | MODIFY | DELETE) != CREATE | MODIFY | DELETE:
            raise RuntimeError("Guest file monitor missed its transient write control")
        print("AUDIT_GUEST_READY", flush=True)
        while True:
            ready, _, _ = select.select([0, watch.fd], [], [])
            for path, mask in watch.read():
                if path == artifact or path.is_relative_to("/tmp"):
                    continue
                if mask & CHANGE:
                    print(f"AUDIT_GUEST_WRITE {path}", flush=True)
            if 0 in ready:
                if sys.stdin.readline() != "finish\n":
                    return 1
                remaining = set()
                for root in roots:
                    remaining.update(watch.tree(root))
                for added in remaining - initial - {artifact}:
                    if not added.is_relative_to("/tmp"):
                        print(f"AUDIT_GUEST_WRITE {added}", flush=True)
                print("AUDIT_GUEST_DONE", flush=True)
                return 0
    finally:
        watch.close()


if __name__ == "__main__":
    try:
        sys.exit(guest_watch())
    except (OSError, RuntimeError):
        sys.exit("Guest file monitor failed")
