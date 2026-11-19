"""Only one WorldBaton per Windows account. Opening it again (from the Start menu, a shortcut or the .exe) brings the
one already running to the front, even when it's hidden in the tray, instead of starting a second copy that would
fight over the same sync engine and settings.

A named mutex says who is first. The first WorldBaton also listens on a local port (written to instance-port) for
"show"; a later one sends that and quits.
"""
import ctypes
import hashlib
import os
import queue
import socket
import threading
from pathlib import Path

from .app_paths import APP_DIR

WAIT_OBJECT_0, WAIT_ABANDONED = 0, 0x80


class Instance:
    def __init__(self, app_dir: Path = APP_DIR):
        self.port_file = Path(app_dir) / "instance-port"
        self.name = "Local\\WorldBaton-" + hashlib.sha1(str(app_dir).lower().encode()).hexdigest()[:16]
        self.shows: queue.Queue[str] = queue.Queue()  # "show" requests from later copies, for the window to read
        self.mutex = self.server = None

    def claim(self, wait: float = 0) -> bool:
        """True if this is the only WorldBaton (waiting up to `wait` seconds for one that is closing)."""
        if os.name != "nt":
            return True
        k32 = ctypes.windll.kernel32
        if not self.mutex:
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.WaitForSingleObject.restype = ctypes.c_uint32
            self.mutex = k32.CreateMutexW(None, False, self.name)
        return k32.WaitForSingleObject(self.mutex, int(wait * 1000)) in (WAIT_OBJECT_0, WAIT_ABANDONED)

    def listen(self) -> None:
        """As the first WorldBaton: take "show" requests from later copies."""
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        self.port_file.write_text(str(self.server.getsockname()[1]))
        threading.Thread(target=self._serve, args=(self.server,), daemon=True).start()

    def _serve(self, server) -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return  # closed by stop()
            with conn:
                conn.settimeout(2)
                try:
                    if conn.recv(16) == b"show":
                        self.shows.put("show")
                        conn.sendall(b"ok")
                except OSError:
                    pass

    def show_other(self) -> bool:
        """As a later copy: ask the running WorldBaton to come to the front. False if it didn't answer."""
        try:
            port = int(self.port_file.read_text().strip())
            if os.name == "nt":
                ctypes.windll.user32.AllowSetForegroundWindow(-1)  # let it take the focus from us
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                s.sendall(b"show")
                return s.recv(2) == b"ok"
        except (OSError, ValueError):
            return False

    def stop(self) -> None:
        """On closing (also before restarting after an update): let the next WorldBaton be the first."""
        if self.server:
            self.server.close()
            self.server = None
            self.port_file.unlink(missing_ok=True)
        if self.mutex and os.name == "nt":
            ctypes.windll.kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.ReleaseMutex(self.mutex)
