"""The WorldBaton icon in the Windows notification area (system tray), and the notifications it shows.

pystray runs its own thread, and tkinter must only be touched from its own thread, so menu clicks are put on a
queue that the window reads (App.poll_tray).
"""
import queue

from . import app_paths


class Tray:
    def __init__(self):
        self.clicks: queue.Queue[str] = queue.Queue()
        self.icon = None

    def start(self) -> bool:
        """Show the tray icon. False if it can't (e.g. pystray missing in a source checkout)."""
        try:
            import pystray
            from PIL import Image
            image = Image.open(app_paths.ICON)
        except Exception:
            return False
        menu = pystray.Menu(pystray.MenuItem("Open WorldBaton", lambda: self.clicks.put("open"), default=True),
                            pystray.MenuItem("Quit", lambda: self.clicks.put("quit")))
        self.icon = pystray.Icon("WorldBaton", image, "WorldBaton", menu)
        self.icon.run_detached()
        return True

    def notify(self, title: str, message: str) -> None:
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass  # notifications are a nicety; never let one break the app

    def stop(self) -> None:
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
