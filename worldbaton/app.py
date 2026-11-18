"""WorldBaton window (tkinter)."""
import ctypes
import json
import os
import sys
import threading
import time
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import sv_ttk

from . import __version__, changelog, core, tools, updater
from .app_paths import APP_DIR, ICON
from .tray import Tray
from .syncthing import Syncthing

CONFIG = APP_DIR / "config.json"
SQUID_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SquidServers"
REFRESH_MS = 3000


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def dark_title_bar(win) -> None:
    """Make the Windows title bar dark to match the theme (Windows 10 20H1+ and 11; ignored elsewhere)."""
    try:
        win.update()  # a pop-up's outer frame (which owns the title bar) only exists once it's on screen
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        if win.winfo_ismapped():  # Windows only repaints the title bar when the window is redrawn
            win.withdraw()
            win.deiconify()
    except Exception:
        pass


DEFAULT_SETTINGS = {"notify": True, "tray": True}


class App:
    def __init__(self, root: tk.Tk, hidden=False):
        self.root, self.cfg, self.group, self.st = root, load_config(), None, None
        self.cfg["settings"] = {**DEFAULT_SETTINGS, **self.cfg.get("settings", {})}
        self.busy = False
        self.prev_status = self.notified_changes = self.activity_shown = None
        root.title(f"WorldBaton {__version__}")
        root.minsize(520, 460)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True)
        self.body = ttk.Frame(self.tabs, padding=16)
        self.history_tab = ttk.Frame(self.tabs, padding=16)
        self.settings_tab = ttk.Frame(self.tabs, padding=16)
        self.guide = ttk.Frame(self.tabs, padding=16)
        self.news = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(self.body, text="My group")
        self.tabs.add(self.history_tab, text="History")
        self.tabs.add(self.settings_tab, text="Settings")
        self.tabs.add(self.guide, text="Setup guide")
        self.tabs.add(self.news, text="What's new")
        self.tabs.bind("<<NotebookTabChanged>>", lambda _: self.refresh_history())
        self.build_guide()
        self.tray = Tray()
        self.tray.start()  # the icon also carries notifications; without pystray, closing just quits
        self.poll_tray()
        if hidden and self.tray.icon:
            root.withdraw()  # started with Windows: stay in the tray
        else:
            self.root.after(300, self.show_whats_new)
        ttk.Label(self.body, text="Starting sync engine…").pack()
        self.error = None
        threading.Thread(target=self._start_engine, daemon=True).start()
        self.root.after(200, self._wait_engine)

    # --- startup -----------------------------------------------------------------
    def _start_engine(self):
        try:
            st = Syncthing(APP_DIR / "syncthing")
            st.start()
            self.st = st
        except Exception as e:  # shown in the window
            self.error = e

    def _wait_engine(self):
        if self.error:
            messagebox.showerror("WorldBaton", f"Couldn't start the sync engine:\n{self.error}")
            self.shutdown()
            return
        if not self.st:
            self.root.after(200, self._wait_engine)
            return
        if not self.cfg.get("player"):
            self.tabs.select(self.guide)  # first run: show how it works
            name = simpledialog.askstring("WorldBaton", "Your name (friends will see this):", parent=self.root)
            self.cfg["player"] = (name or os.environ.get("USERNAME") or "Player").strip()[:32]
            save_config(self.cfg)
        self.st.set_name(self.cfg["player"])
        if self.cfg.get("group"):
            self.group = core.Group(self.st, self.cfg["group"], APP_DIR)
            self.st.set_ignores(self.group.world_id, core.IGNORES)  # groups made by older versions get new rules
        self.show()
        self.check_updates()

    # --- screens -----------------------------------------------------------------
    GUIDE = [
        ("h", "Before you start"),
        ("p", "• WorldBaton is an add-on for SquidServers, not a replacement: it doesn't run the server. You start "
              "and stop the server in SquidServers, which also gives you the address friends join with. WorldBaton "
              "passes the world and the turn to host between you."),
        ("p", "• Only use WorldBaton with people you'd trust with full access to your computer. Anyone in your group "
              "can put mods and files on your PC, and mods are programs that run when you host or play with them."),
        ("p", "• Everyone who wants to take turns hosting needs WorldBaton and SquidServers. "
              "Friends who only play just need Minecraft."),
        ("p", "• Make a copy of your server folder before the first time, just in case."),
        ("h", "Starting a group (the owner)"),
        ("p", "1. Stop your server in SquidServers."),
        ("p", "2. In My group, press Create a group from my server and pick your server folder "
              "(SquidServers keeps them in %LOCALAPPDATA%\\SquidServers)."),
        ("p", "3. Press Invite a friend and send them the invite code."),
        ("p", "4. Paste the join code they send back and press Add friend."),
        ("h", "Joining a friend's group"),
        ("p", "1. Press Join a friend's group and paste their invite code."),
        ("p", "2. Send the join code WorldBaton shows back to your friend."),
        ("p", "3. Wait for the world to copy over (your friend's list shows you at 100%)."),
        ("p", "4. In SquidServers, create a new server with the SAME type and Minecraft version as the group's "
              "(for example NeoForge 1.21.1) and a different name. Don't add mods. Let it install, then keep it stopped."),
        ("p", "5. Back in My group, press Change server folder… and pick that new server's folder. "
              "WorldBaton fills it with the group's world, mods and settings."),
        ("h", "Taking turns hosting"),
        ("p", "1. Press Host now. The first time, paste your tunnel address from SquidServers "
              "(change it later with My address…)."),
        ("p", "2. Start the server in SquidServers. Friends see your address in WorldBaton."),
        ("p", "3. When you're done, stop the server in SquidServers FIRST, then press Stop hosting."),
        ("p", "4. Keep WorldBaton open until your friends show 100% synced."),
        ("p", "• Only start the server when WorldBaton says you're the host. Anything played otherwise is undone."),
        ("h", "Taking turns"),
        ("p", "• Someone else hosting and you want to go next? Press Ask to host next. They're told, and so are you "
              "when it's your turn."),
        ("p", "• Turn on Start WorldBaton when I sign in to Windows (Settings), so friends can get the world from you "
              "even when you didn't open it. It waits in the tray."),
        ("p", "• Made a mistake? The History tab can bring back an earlier world for everyone."),
        ("h", "Mods and fair play"),
        ("p", "• When the host adds or removes a mod, everyone sees it under My group. Press Update my game's mods "
              "to copy them into your own Minecraft, or you can't join."),
        ("p", "• Admin commands (op, gamemode, give, tp…) used on the server are listed under Host activity for the whole "
              "group. So are signs someone tried to hide them: an edited server log, files changed while the server "
              "was off, or mods loaded from somewhere else. Those show in red."),
        ("p", "• Operators, datapacks and important server settings are watched too, like mods."),
        ("h", "Rarely online at the same time?"),
        ("p", "• Share a Google Drive, OneDrive or Dropbox folder with the group and install that drive's desktop app."),
        ("p", "• Press Backup folder… and pick your copy of that shared folder. Everyone does this on their own PC."),
        ("p", "• After Stop hosting, wait until the drive app says it's up to date before turning off your PC."),
    ]

    def page(self, parent, items, height=18):
        """Read-only scrolling text: items are ("h", heading) and ("p", paragraph) pairs."""
        text = tk.Text(parent, wrap="word", relief="flat", padx=4, pady=4, height=height,
                       background=self.root.cget("background"), font=("Segoe UI", 10))
        bar = ttk.Scrollbar(parent, command=text.yview)
        text.config(yscrollcommand=bar.set)
        text.tag_config("h", font=("Segoe UI", 11, "bold"), spacing1=10, spacing3=4)
        text.tag_config("p", lmargin1=4, lmargin2=18, spacing3=3)
        for tag, line in items:
            text.insert("end", line + "\n", tag)
        text.config(state="disabled")
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

    @staticmethod
    def changes_page(sections):
        items = []
        for version, changes in sections:
            now = " (this version)" if updater.parse(version) == updater.parse(__version__) else ""
            items += [("h", f"WorldBaton {version}{now}")] + [("p", f"• {c}") for c in changes]
        return items or [("p", "No changes listed.")]

    def build_guide(self):
        foot = ttk.Frame(self.guide)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Label(foot, text=f"WorldBaton {__version__}", foreground="#a0a0a0").pack(side="left")
        ttk.Button(foot, text="Check for updates", command=lambda: self.check_updates(manual=True)).pack(side="right")
        self.page(self.guide, self.GUIDE)
        # versions newer than this one (a source checkout's next release) aren't "what's new" yet
        released = [s for s in changelog.load() if updater.parse(s[0]) <= updater.parse(__version__)]
        self.page(self.news, self.changes_page(released))

    def show_whats_new(self):
        """Once after an update: what changed since the version this PC ran before."""
        last = self.cfg.get("last_version")
        self.cfg["last_version"] = __version__
        save_config(self.cfg)
        if last == __version__ or not self.cfg.get("player"):
            return  # same version, or a brand-new install (the setup guide comes first)
        sections = changelog.between(changelog.load(), last or "0", __version__)
        if not last:  # updated from a version before this existed: just this version's changes
            sections = sections[:1]
        if not sections:
            return
        win = tk.Toplevel(self.root)
        win.title("WorldBaton")
        win.minsize(460, 320)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"What's new in WorldBaton {__version__}", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Button(frame, text="Got it", style="Accent.TButton", command=win.destroy).pack(side="bottom", anchor="e",
                                                                                         pady=(10, 0))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.page(body, self.changes_page(sections), height=12)
        win.transient(self.root)
        self.popup(win)

    def popup(self, win):
        """Show a pop-up over the main window with a dark title bar. Call last: it puts the window on screen."""
        win.geometry(f"+{self.root.winfo_rootx() + 40}+{self.root.winfo_rooty() + 40}")
        dark_title_bar(win)

    def clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def show(self):
        self.clear()
        self.build_settings()
        self.refresh_history()
        if self.group:
            self.show_group()
        else:
            ttk.Label(self.body, text=f"Hi {self.cfg['player']}!", font=("Segoe UI", 14, "bold")).pack(pady=(0, 8))
            ttk.Label(self.body, text="Share one Minecraft server with friends.\nWhoever is online can host it.",
                      justify="center").pack(pady=(0, 6))
            ttk.Label(self.body, text="An add-on for SquidServers: SquidServers runs the server, WorldBaton passes "
                                      "the world and the turn to host between you.", wraplength=420, justify="center",
                      foreground="#a0a0a0").pack(pady=(0, 16))
            ttk.Button(self.body, text="Create a group from my server", command=self.create_group).pack(fill="x", pady=4)
            ttk.Button(self.body, text="Join a friend's group", command=self.join_group).pack(fill="x", pady=4)
            if not SQUID_DIR.exists():  # SquidServers keeps its servers here once it's installed and used
                ttk.Label(self.body, text="SquidServers doesn't seem to be on this PC. You can still join a group and "
                                          "get the world, but to host you'll need SquidServers.", wraplength=420,
                          justify="center", foreground="#ffd479").pack(pady=(16, 6))
                ttk.Button(self.body, text="Get SquidServers",
                           command=lambda: webbrowser.open("https://squidservers.com")).pack()

    def show_group(self):
        b = self.body
        ttk.Label(b, text=self.group.info["name"], font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self.headline = ttk.Label(b, font=("Segoe UI", 11, "bold"), wraplength=440)
        self.headline.pack(anchor="w", pady=(8, 0))
        addr_row = ttk.Frame(b)
        addr_row.pack(anchor="w", fill="x")
        self.address = ttk.Entry(addr_row, width=40)
        self.copy_btn = ttk.Button(addr_row, text="Copy address", command=lambda: self.copy(self.address.get()))
        self.reason = ttk.Label(b, wraplength=440, foreground="#a0a0a0")
        self.reason.pack(anchor="w", pady=(4, 8))
        self.warning = ttk.Label(b, wraplength=440, foreground="#ff8080")
        self.warning.pack(anchor="w")
        self.changes_box = ttk.Frame(b)  # stays packed (empty when nothing changed) so it keeps its place
        self.changes_box.pack(anchor="w", fill="x")
        self.changes = ttk.Label(self.changes_box, wraplength=440, foreground="#ffd479")
        self.changes_row = ttk.Frame(self.changes_box)
        ttk.Button(self.changes_row, text="Got it", command=self.ack_changes).pack(side="left", padx=(0, 6))
        ttk.Button(self.changes_row, text="Update my game's mods", command=self.update_game_mods).pack(side="left",
                                                                                                   padx=(0, 6))
        ttk.Button(self.changes_row, text="Open server mods folder",
                   command=lambda: os.startfile(self.group.world_path / "mods")).pack(side="left")

        ttk.Label(b, text="Friends", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 0))
        self.members = tk.Text(b, height=6, width=56, relief="flat", background=self.root.cget("background"), font=("Segoe UI", 10))
        self.members.pack(anchor="w", fill="x")

        ttk.Label(b, text="Host activity (admin commands)", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 0))
        self.activity = tk.Text(b, height=5, width=56, relief="flat", wrap="word", background=self.root.cget("background"),
                                font=("Segoe UI", 10))
        self.activity.pack(anchor="w", fill="x")
        self.activity_shown = None

        row = ttk.Frame(b)
        row.pack(fill="x", pady=(12, 0))
        self.host_btn = ttk.Button(row, text="Host now", command=self.host)
        self.stop_btn = ttk.Button(row, text="Stop hosting", command=self.stop)
        self.discard_btn = ttk.Button(row, text="Discard my session", command=self.discard)
        self.ask_btn = ttk.Button(row, text="Ask to host next", command=self.toggle_request)
        for w in (self.host_btn, self.stop_btn, self.discard_btn, self.ask_btn):
            w.pack(side="left", padx=(0, 6))
        row2 = ttk.Frame(b)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="Invite a friend", command=self.invite).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Open server folder", command=lambda: os.startfile(self.group.world_path)).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Backup folder…", command=self.pick_backup).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Change server folder…", command=self.change_folder).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="My address…", command=self.ask_address).pack(side="left")
        self.backup_label = ttk.Label(b, wraplength=440, foreground="#a0a0a0")
        self.backup_label.pack(anchor="w", pady=(8, 0))
        self.show_backup()
        self.tick()
        self.beat()

    # --- refresh -----------------------------------------------------------------
    def tick(self):
        if not self.group or not self.headline.winfo_exists():
            return
        if self.busy:  # never reconcile while a restore/backup is touching the server folder
            self.root.after(REFRESH_MS, self.tick)
            return
        try:
            self.group.reconcile()
            s = self.group.status()
        except Exception as e:
            self.reason.config(text=f"Sync engine busy ({e}). Retrying…")
            self.root.after(REFRESH_MS, self.tick)
            return
        self.last_status = s
        if s["me_hosting"]:
            try:  # every refresh, so there's little time to edit the log before it's read
                self.group.record_activity(self.cfg["player"])
                self.group.watch_offline_edits(self.cfg["player"])
                if self.group.update_server_up() == "up":
                    self.post_discord(f"🟢 **{self.cfg['player']}** is hosting **{self.group.info['name']}**. "
                                      f"Join at `{self.cfg.get('address') or 'ask them for the address'}`")
            except Exception:
                pass
        for title, message in core.events(self.prev_status, s, self.st.my_id):
            self.notify(title, message)
        self.prev_status = s
        baton = s["baton"] or {}
        self.address.pack_forget()
        self.copy_btn.pack_forget()
        if s["me_hosting"]:
            self.headline.config(text="You are hosting. Keep WorldBaton open while the server runs." if s["server_running"] else
                                 "You are hosting. Start the server in SquidServers, or press Stop hosting to share your session.")
        elif s["holder_alive"]:
            self.headline.config(text=f"{baton.get('holder_name', 'Someone')} is hosting. Join at:")
            self.address.config(state="normal")
            self.address.delete(0, "end")
            self.address.insert(0, baton.get("address") or "(no address given)")
            self.address.config(state="readonly")
            self.address.pack(side="left")
            self.copy_btn.pack(side="left", padx=6)
        else:
            self.headline.config(text="Nobody is hosting.")
        if s["me_hosting"]:
            asking = s["requests"]
            self.reason.config(text=f"{' and '.join(asking)} {'wants' if len(asking) == 1 else 'want'} to host next. "
                                    "When you're done, stop the server in SquidServers, then press Stop hosting."
                               if asking else "")
        else:
            self.reason.config(text=s["reason"] + (f" ({s['need_bytes'] / 1e6:.0f} MB left)" if s["need_bytes"] else "")
                               + ("\nYou asked to host next. You'll be told when it's your turn."
                                  if s["my_request"] else ""))

        warns = []
        if s["lost_baton"]:
            warns.append("Someone else took over while you were hosting. Your session is kept on this PC only.")
        if s["conflicts"]:
            warns.append("Two people changed the baton at the same time. Check who is really hosting before playing.")
        if s["server_running"] and not s["me_hosting"]:
            warns.append("Your server is running but you aren't the host! Stop it, or your changes will be lost.")
        self.show_changes(s)
        if self.snapshot:
            warns += core.watch_warnings(self.snapshot)
        self.warning.config(text="\n".join(warns))

        self.members.config(state="normal")
        self.members.delete("1.0", "end")
        for m in s["members"] or []:
            sync = "" if m["synced"] is None else f", {m['synced']:.0f}% synced"
            self.members.insert("end", f"{'●' if m['online'] else '○'} {m['name']} — {'online' if m['online'] else 'offline'}{sync}\n")
        if not s["members"]:
            self.members.insert("end", "No friends yet. Press 'Invite a friend'.")
        self.members.config(state="disabled")
        self.show_activity()

        self.host_btn.config(state="normal" if s["can_host"] else "disabled")
        self.stop_btn.config(state="normal" if s["me_hosting"] else "disabled")
        self.discard_btn.config(state="normal" if s["lost_baton"] else "disabled")
        someone_else = s["holder_alive"] and not s["me_hosting"]
        self.ask_btn.config(text="Cancel my request" if s["my_request"] else "Ask to host next",
                            state="normal" if someone_else or s["my_request"] else "disabled")
        self.root.after(REFRESH_MS, self.tick)

    def show_changes(self, s):
        """Mods/operators that changed since this player last pressed Got it."""
        self.snapshot = None
        if s["need_files"] or not s["local_version"]:  # still downloading: wait for the full picture
            changes = []
        else:
            self.snapshot = core.server_snapshot(self.group.world_path)
            if "seen" not in self.group.info:  # first look (or just joined and synced): nothing is "new"
                self.ack_changes()
            changes = core.snapshot_changes(self.group.info["seen"], self.snapshot)
        if self.notified_changes is not None and changes and set(changes) - set(self.notified_changes):
            self.notify("The server changed", "\n".join(changes[:3]) + ("\n…" if len(changes) > 3 else ""))
        self.notified_changes = changes
        if not changes:
            self.changes.pack_forget()
            self.changes_row.pack_forget()
            return
        more = f"\n…and {len(changes) - 8} more" if len(changes) > 8 else ""
        self.changes.config(text="Changed since you last looked:\n" + "\n".join(changes[:8]) + more +
                            ("\nAdd/remove the same mods in your own Minecraft to keep joining."
                             if any(c.startswith(("New mod", "Mod removed")) for c in changes) else ""))
        self.changes.pack(anchor="w", pady=(8, 0))
        self.changes_row.pack(anchor="w", pady=(4, 0))

    def ack_changes(self):
        if self.snapshot is not None:
            self.group.info["seen"] = self.snapshot
            save_config(self.cfg)
            self.changes.pack_forget()
            self.changes_row.pack_forget()

    def show_activity(self):
        entries = self.group.activity()
        if entries == self.activity_shown:  # don't reset the scroll position every refresh
            return
        if self.activity_shown is not None:
            before = {(e.get("t"), e.get("text"), bool(e.get("removed"))) for e in self.activity_shown}
            for e in entries:
                flagged = e.get("removed") or e.get("text", "").startswith("⚠")
                if flagged and (e.get("t"), e.get("text"), bool(e.get("removed"))) not in before:
                    self.notify("Host activity", e.get("text", "").lstrip("⚠ ") +
                                (" (deleted from the host's list)" if e.get("removed") else ""))
        self.activity_shown = entries
        self.activity.config(state="normal")
        self.activity.delete("1.0", "end")
        self.activity.tag_config("warn", foreground="#ff8080")
        for e in entries:
            when = time.strftime("%d %b %H:%M", time.localtime(e.get("t", 0)))
            who = f"hosted by {e['host']} — " if e.get("host") else ""
            gone = "  (deleted from the host's list since)" if e.get("removed") else ""
            warn = e.get("removed") or e.get("text", "").startswith("⚠")
            self.activity.insert("end", f"{when}  {who}{e.get('text', '')}{gone}\n", ("warn",) if warn else ())
        if not entries:
            self.activity.insert("end", "Nothing yet. Commands like /op, /gamemode and /give show up here.")
        self.activity.config(state="disabled")

    def beat(self):
        if self.group and not self.busy:
            try:
                self.group.heartbeat()
            except Exception:
                pass
        if self.group:
            self.root.after(core.HEARTBEAT_EVERY * 1000, self.beat)

    # --- actions -----------------------------------------------------------------
    def create_group(self):
        start = SQUID_DIR if SQUID_DIR.exists() else Path.home()
        path = filedialog.askdirectory(title="Pick your server folder", initialdir=start, mustexist=True)
        if not path:
            return
        name = simpledialog.askstring("WorldBaton", "Group name:", initialvalue=Path(path).name, parent=self.root)
        if not name:
            return
        try:
            self.group = core.Group.create(self.st, APP_DIR, name.strip(), Path(path), self.cfg["player"])
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))
            return
        self.cfg["group"] = self.group.info
        save_config(self.cfg)
        self.show()
        self.invite()

    def join_group(self):
        code = simpledialog.askstring("WorldBaton", "Paste the invite code your friend sent you:", parent=self.root)
        if not code:
            return
        try:
            invite = core.decode_invite(code)
            parent = SQUID_DIR if SQUID_DIR.exists() else Path.home() / "WorldBaton servers"
            self.group = core.Group.join(self.st, APP_DIR, invite, parent)
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))
            return
        self.cfg["group"] = self.group.info
        save_config(self.cfg)
        self.show()
        self.code_dialog(f"Send this code back to {invite['on']} so they can let you in:",
                         core.encode_join(self.st.my_id, self.cfg["player"]))

    def invite(self):
        win = self.code_dialog("1. Send this invite code to your friend:", self.group.invite_code(self.cfg["player"]))
        ttk.Label(win, text="2. Paste the join code they send back:").pack(anchor="w", padx=12, pady=(12, 0))
        entry = ttk.Entry(win, width=60)
        entry.pack(padx=12, fill="x")

        def add():
            try:
                join = core.decode_join(entry.get())
                self.group.add_member(join)
            except Exception as e:
                messagebox.showerror("WorldBaton", str(e), parent=win)
                return
            messagebox.showinfo("WorldBaton", f"{join['n']} is in! Their copy of the world will start syncing.", parent=win)
            win.destroy()

        ttk.Button(win, text="Add friend", command=add).pack(pady=12)

    def ask_address(self) -> bool:
        """Ask for this PC's tunnel address and remember it. False if cancelled."""
        msg = ("Paste your server address from SquidServers (the tunnel address friends join with).\n"
               "WorldBaton remembers it. You can leave it empty and tell friends yourself.")
        addr = simpledialog.askstring("My address", msg, initialvalue=self.cfg.get("address", ""), parent=self.root)
        if addr is None:
            return False
        self.cfg["address"] = addr.strip()
        save_config(self.cfg)
        baton = self.group.read_baton() if self.group else None
        if baton and baton.get("holder") == self.st.my_id:  # hosting now: show friends the new address
            self.group.write_baton({**baton, "address": self.cfg["address"]})
        return True

    def host(self):
        problems = tools.prehost_problems(self.group.world_path)
        if problems and not messagebox.askyesno("Before you host", "\n\n".join(problems) + "\n\nHost anyway?",
                                                icon="warning"):
            return
        if "address" not in self.cfg and not self.ask_address():
            return
        addr = self.cfg["address"]
        self.run_busy("Getting the world ready…", lambda: self.group.start_hosting(self.cfg["player"], addr),
                      lambda _: messagebox.showinfo("WorldBaton", "You're the host. Start the server in SquidServers now.\n"
                                                    "When you're done: stop the server there, then press 'Stop hosting' here."))

    def stop(self):
        def done(warning):
            if warning:
                messagebox.showwarning("WorldBaton", warning)
            tip = ("If you use a cloud drive as the backup folder, wait until it says it's up to date before turning off your PC."
                   if self.group.backup_dir else "")
            messagebox.showinfo("WorldBaton", "Done! Keep WorldBaton open until your friends show 100% synced.\n" + tip)
            self.post_discord(f"⚪ **{self.cfg['player']}** stopped hosting **{self.group.info['name']}**. "
                              "Their session is being shared with the group.")
            self.refresh_history()
        self.run_busy("Sharing your session…", lambda: self.group.stop_hosting(self.cfg["player"]), done)

    def pick_backup(self):
        msg = ("A backup folder lets friends host even when you're never online at the same time.\n\n"
               "Pick a folder everyone in the group syncs, like a shared Google Drive, OneDrive or Dropbox folder. "
               "Each friend picks their own copy of that same shared folder.\n\nChoose a folder now? (Cancel = turn backups off)")
        if not messagebox.askokcancel("Backup folder", msg):
            self.group.set_backup_dir(None)
        else:
            path = filedialog.askdirectory(title="Pick the shared backup folder", mustexist=True)
            if not path:
                return
            self.group.set_backup_dir(Path(path))
        self.cfg["group"] = self.group.info
        save_config(self.cfg)
        self.show_backup()

    def change_folder(self):
        msg = ("To play the group's world in SquidServers:\n\n"
               "1. In SquidServers, create a new server with the SAME type and Minecraft version as the group's "
               "(give it a different name). Don't add mods or a modpack.\n"
               "2. Make sure it's stopped, then pick its folder here.\n\n"
               "WorldBaton will replace its world, mods and settings with the group's. "
               "Its own start files, RAM setting and logs are kept.")
        if not messagebox.askokcancel("Change server folder", msg):
            return
        start = SQUID_DIR if SQUID_DIR.exists() else Path.home()
        path = filedialog.askdirectory(title="Pick the server folder SquidServers made", initialdir=start, mustexist=True)
        if not path:
            return

        def done(_):
            self.cfg["group"] = self.group.info
            save_config(self.cfg)
            messagebox.showinfo("WorldBaton", f"Done! The group's world now lives in:\n{path}\n\n"
                                "You can start it from SquidServers when it's your turn to host.")
        self.run_busy("Copying the group's files…", lambda: self.group.move_world(Path(path)), done)

    def show_backup(self):
        d = self.group.backup_dir
        self.backup_label.config(text=f"Backup folder: {d}" if d else
                                 "No backup folder. Only needed if your group is rarely online at the same time.")

    def run_busy(self, text, work, on_done):
        """Run slow work (zipping/unzipping worlds) off the UI thread."""
        self.busy, result = True, {}
        self.reason.config(text=text)
        for w in (self.host_btn, self.stop_btn, self.discard_btn):
            w.config(state="disabled")

        def run():
            try:
                result["ok"] = work()
            except Exception as e:
                result["err"] = e
        threading.Thread(target=run, daemon=True).start()

        def poll():
            if not result:
                self.root.after(300, poll)
                return
            self.busy = False
            if "err" in result:
                messagebox.showerror("WorldBaton", str(result["err"]))
            else:
                on_done(result["ok"])
        poll()

    def discard(self):
        if not messagebox.askyesno("WorldBaton", "Throw away your session and download the group's current world?"):
            return
        try:
            self.group.discard_session()
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))

    # --- tray, notifications, Discord ----------------------------------------------
    def notify(self, title, message):
        if self.cfg["settings"]["notify"]:
            self.tray.notify(title, message)

    def poll_tray(self):
        """Handle tray menu clicks here, on tkinter's thread."""
        while not self.tray.clicks.empty():
            click = self.tray.clicks.get()
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            if click == "quit":
                self.quit()
                return
        self.root.after(300, self.poll_tray)

    def post_discord(self, text):
        url = self.group.group_settings().get("discord_webhook") if self.group else None
        if url:  # in the background: a slow or broken webhook must never freeze the window
            threading.Thread(target=lambda: self._try_post(url, text), daemon=True).start()

    @staticmethod
    def _try_post(url, text):
        try:
            tools.post_discord(url, text)
        except Exception:
            pass

    # --- asking to host next, your game's mods -----------------------------------------
    def toggle_request(self):
        try:
            if self.group.my_request():
                self.group.cancel_request()
            else:
                self.group.request_host(self.cfg["player"])
        except OSError as e:
            messagebox.showerror("WorldBaton", str(e))

    def pick_game_mods(self) -> bool:
        start = tools.default_game_mods()
        msg = ("Pick the mods folder of the Minecraft you play on.\n\n"
               f"For the normal launcher it's {start}. For CurseForge, Prism or Modrinth, it's the mods folder "
               "inside that profile's (instance's) folder.")
        if not messagebox.askokcancel("My game's mods", msg):
            return False
        path = filedialog.askdirectory(title="Pick your game's mods folder",
                                       initialdir=start if start.exists() else start.parent.parent)
        if not path:
            return False
        self.group.info["game_mods"] = {"dir": path, "managed": []}
        save_config(self.cfg)
        self.build_settings()
        return True

    def update_game_mods(self):
        mods = self.group.info.get("game_mods") or {}
        if not mods.get("dir") and not self.pick_game_mods():
            return
        mods = self.group.info["game_mods"]
        if not mods.get("managed") and not messagebox.askokcancel(
                "My game's mods", "WorldBaton will copy the server's mods into:\n" + mods["dir"] + "\n\nMods you added "
                "yourself stay. Mods the server drops later are removed again.\n\nA few mods only work on servers. If "
                "your game won't start afterwards, remove that mod from the folder."):
            return

        def done(result):
            added, removed, managed = result
            mods["managed"] = managed
            save_config(self.cfg)
            messagebox.showinfo("WorldBaton", "Your game's mods match the server now."
                                f"\nAdded or updated: {len(added)}\nRemoved: {len(removed)}")
        self.run_busy("Copying mods…", lambda: tools.sync_game_mods(self.group.world_path / "mods", Path(mods["dir"]),
                                                                    mods.get("managed", [])), done)

    # --- Settings tab --------------------------------------------------------------
    def build_settings(self):
        f = self.settings_tab
        for w in f.winfo_children():
            w.destroy()
        s = self.cfg["settings"]

        def heading(text):
            ttk.Label(f, text=text, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(10, 2))

        def note(text):
            ttk.Label(f, text=text, wraplength=470, foreground="#a0a0a0").pack(anchor="w", padx=(28, 0))

        def toggle(text, key):
            var = tk.BooleanVar(value=s[key])

            def changed():
                s[key] = var.get()
                save_config(self.cfg)
            ttk.Checkbutton(f, text=text, variable=var, command=changed).pack(anchor="w")

        heading("This PC")
        toggle("Show notifications", "notify")
        note("When someone starts hosting, their server is up, someone asks to host next, or the server changes.")
        toggle("Keep running in the tray when I close the window", "tray")
        auto = tk.BooleanVar(value=tools.autostart_enabled())

        def set_auto():
            try:
                tools.set_autostart(auto.get())
            except OSError as e:
                auto.set(not auto.get())
                messagebox.showerror("WorldBaton", str(e))
        ttk.Checkbutton(f, text="Start WorldBaton when I sign in to Windows", variable=auto, command=set_auto,
                        state="normal" if tools.can_autostart() else "disabled").pack(anchor="w")
        note("It starts in the tray, so friends can get the world from you even when you didn't open it.")
        if not self.group:
            return

        heading("World backups")
        keep = tk.BooleanVar(value=self.group.keep_local > 0)

        def set_keep():
            self.group.info["local_backups"] = core.KEEP_LOCAL if keep.get() else 0
            save_config(self.cfg)
        ttk.Checkbutton(f, text=f"Keep the last {core.KEEP_LOCAL} worlds I host, so they can be restored", variable=keep,
                        command=set_keep).pack(anchor="w")
        note("Saved when you press Stop hosting. Each one is about the size of the server folder. Restore one from "
             "the History tab.")

        heading("My game's mods")
        mods = (self.group.info.get("game_mods") or {}).get("dir")
        ttk.Label(f, text=mods or "Not set yet.", wraplength=470).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(4, 0))
        ttk.Button(row, text="Choose folder…", command=self.pick_game_mods).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Update my game's mods", command=self.update_game_mods).pack(side="left")

        heading("Discord (whole group)")
        hook = tk.StringVar(value=self.group.group_settings().get("discord_webhook", ""))
        ttk.Entry(f, textvariable=hook, width=60, show="•").pack(anchor="w", fill="x")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(4, 0))

        def save_hook():
            url = hook.get().strip()
            if url and not tools.valid_webhook(url):
                messagebox.showerror("WorldBaton", "That isn't a Discord webhook link. In Discord: Server Settings → "
                                     "Integrations → Webhooks → New Webhook → Copy Webhook URL.")
                return
            self.group.set_group_setting("discord_webhook", url)
            messagebox.showinfo("WorldBaton", "Saved for the whole group." if url else "Discord posts are off.")

        def test_hook():
            try:
                tools.post_discord(hook.get(), f"👋 WorldBaton test from {self.cfg['player']}.")
                messagebox.showinfo("WorldBaton", "Sent! Check your Discord channel.")
            except Exception as e:
                messagebox.showerror("WorldBaton", f"Couldn't post: {e}")
        ttk.Button(row, text="Save", command=save_hook).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Send a test", command=test_hook).pack(side="left")
        note("Posts in your channel when someone's server is up (with the address) and when they stop. Anyone in "
             "the group can change it.")

    # --- History tab -----------------------------------------------------------------
    def refresh_history(self):
        f = self.history_tab
        if self.tabs.select() != str(f) and getattr(self, "history_built", False):
            return  # only rebuilt while you look at it
        self.history_built = True
        for w in f.winfo_children():
            w.destroy()
        if not self.group:
            ttk.Label(f, text="Join or create a group first.").pack(anchor="w")
            return
        backups = {b["version"]: b for b in self.group.backups()}
        sessions = self.group.history()
        seen = {e.get("version") for e in sessions}
        rows = [(e.get("end", 0), e) for e in sessions] + [(b["path"].stat().st_mtime, {"version": v})
                                                           for v, b in backups.items() if v not in seen]
        ttk.Label(f, text="Every finished session, newest first. Versions with a backup can be brought back for "
                          "everyone.", wraplength=470).pack(anchor="w", pady=(0, 8))
        bottom = ttk.Frame(f)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        tree = ttk.Treeview(f, columns=("when", "host", "length", "version", "backup"), show="headings", height=12)
        for col, title, width in (("when", "Ended", 120), ("host", "Hosted by", 110), ("length", "Played", 70),
                                  ("version", "Version", 60), ("backup", "Backup", 110)):
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w")
        for end, e in sorted(rows, key=lambda r: r[0], reverse=True):
            v = e.get("version")
            length = ""
            if e.get("start") and e.get("end"):
                mins = int((e["end"] - e["start"]) // 60)
                length = f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"
            host = e.get("host", "?") + (f" (restored v{e['restored_from']})" if e.get("restored_from") else "")
            b = backups.get(v)
            tree.insert("", "end", iid=str(v), values=(
                time.strftime("%d %b %H:%M", time.localtime(end)), host if e.get("host") else "(older session)",
                length, v, f"{b['where']}, " + (f"{b['size'] / 1e9:.1f} GB" if b["size"] >= 1e9 else f"{b['size'] / 1e6:.0f} MB") if b else ""))
        tree.pack(fill="both", expand=True)
        if not rows:
            ttk.Label(f, text="Nothing yet. Sessions show up here after someone presses Stop hosting.").pack(anchor="w")

        def restore():
            sel = tree.selection()
            b = backups.get(int(sel[0])) if sel else None
            if not b:
                messagebox.showinfo("WorldBaton", "Pick a version that has a backup.")
                return
            if not messagebox.askyesno("Restore an earlier world", f"Bring back version {b['version']} for everyone?"
                                       "\n\nThe current world isn't lost: it stays in the list (if it has a backup) "
                                       "and in your Syncthing file versions. Everyone gets the restored world as a "
                                       "new version.", icon="warning"):
                return

            def done(warning):
                if warning:
                    messagebox.showwarning("WorldBaton", warning)
                messagebox.showinfo("WorldBaton", f"Version {b['version']} is back, shared as a new version. Keep "
                                    "WorldBaton open until your friends show 100% synced.")
                self.history_built = False
                self.refresh_history()
            self.tabs.select(self.body)
            self.run_busy("Restoring the world…", lambda: self.group.restore_version(self.cfg["player"], b), done)
        ttk.Button(bottom, text="Restore this version…", command=restore).pack(side="left")
        ttk.Button(bottom, text="Refresh", command=lambda: (setattr(self, "history_built", False),
                                                            self.refresh_history())).pack(side="left", padx=6)

    # --- helpers -----------------------------------------------------------------
    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def code_dialog(self, label, code):
        win = tk.Toplevel(self.root)
        win.title("WorldBaton")
        ttk.Label(win, text=label, wraplength=420).pack(anchor="w", padx=12, pady=(12, 4))
        e = ttk.Entry(win, width=60)
        e.insert(0, code)
        e.config(state="readonly")
        e.pack(padx=12, fill="x")
        ttk.Button(win, text="Copy", command=lambda: self.copy(code)).pack(pady=(6, 0))
        self.popup(win)
        return win

    def on_close(self):
        """The window's X: keep running in the tray (if that's on), so syncing and notifications carry on."""
        if self.cfg["settings"]["tray"] and self.tray.icon:
            self.root.withdraw()
            if not self.cfg.get("told_tray"):
                self.tray.notify("WorldBaton is still running", "It keeps syncing in the tray. Right-click the "
                                 "icon to quit, or turn this off in Settings.")
                self.cfg["told_tray"] = True
                save_config(self.cfg)
            return
        self.quit()

    def quit(self):
        s = getattr(self, "last_status", None)
        if s and s["me_hosting"]:
            if not messagebox.askyesno("WorldBaton", "You're hosting. If you close WorldBaton, friends can take over "
                                       "after 5 minutes and your session won't be shared. Close anyway?"):
                return
        elif s and s["baton"] and s["baton"].get("last_host") == self.st.my_id and any(
                m["online"] and (m["synced"] or 0) < 100 for m in s["members"]):
            if not messagebox.askyesno("WorldBaton", "Your friends don't have your latest world yet. Close anyway?"):
                return
        self.shutdown()

    def shutdown(self):
        self.tray.stop()
        if self.st:
            if self.group:
                try:
                    self.group.park()
                except Exception:
                    pass  # engine already gone; nothing can be sent anyway
            self.st.stop()
        self.root.destroy()

    # --- updates -----------------------------------------------------------------
    def check_updates(self, manual=False):
        """On start: quietly offer an update if there is one. From the button: also say when there isn't."""
        if not manual:
            updater.clean_old()
        if not updater.can_update():
            if manual:
                messagebox.showinfo("WorldBaton", "Updates only work in the downloaded WorldBaton.exe.")
            return
        found = {}

        def run():
            try:
                found["r"] = updater.latest()
            except Exception as e:
                found["r"], found["err"] = None, e  # offline or GitHub unreachable: try again next start
        threading.Thread(target=run, daemon=True).start()

        def poll():
            if "r" not in found:
                self.root.after(500, poll)
            elif found["r"]:
                self.offer_update(*found["r"])
            elif manual and "err" in found:
                messagebox.showerror("WorldBaton", f"Couldn't check for updates. Are you online?\n({found['err']})")
            elif manual:
                messagebox.showinfo("WorldBaton", f"You're up to date (WorldBaton {__version__}).")
        poll()

    def offer_update(self, tag, url, notes=""):
        s = getattr(self, "last_status", None)
        if s and s["me_hosting"]:
            messagebox.showinfo("WorldBaton", f"WorldBaton {tag} is out. It'll offer to update next time you open it "
                                "when you're not hosting.")
            return
        lines = [ln for ln in notes.splitlines() if ln.strip()]
        shown = "\n".join(lines[:10]) + ("\n..." if len(lines) > 10 else "")
        if not messagebox.askyesno("WorldBaton", f"WorldBaton {tag} is out (you have {__version__}).\n\n"
                                   + (f"What's new:\n{shown}\n\n" if shown else "")
                                   + "Update now? WorldBaton will restart."):
            return
        done = {}

        def run():
            try:
                updater.download_and_install(url)
                done["ok"] = True
            except Exception as e:
                done["err"] = e
        threading.Thread(target=run, daemon=True).start()
        self.root.title(f"WorldBaton {__version__} (downloading update…)")

        def poll():
            if not done:
                self.root.after(500, poll)
            elif "err" in done:
                self.root.title(f"WorldBaton {__version__}")
                messagebox.showerror("WorldBaton", f"Update failed: {done['err']}")
            else:
                self.shutdown()
                updater.restart()
        poll()


RISK_VERSION = 1  # raise to show the agreement to everyone again after changing it
RISK_TEXT = (
    "WorldBaton keeps a copy of your group's server folder on this PC and keeps it in sync with everyone in the "
    "group. That means anyone in your group can put files on your computer: mods, configs, scripts, anything in "
    "the server folder.\n\n"
    "Minecraft mods are programs. A harmful mod or file from someone in your group would run on this PC when you "
    "host the server, or when you copy the server's mods into your own Minecraft. It would have the same access "
    "you have: your files, your accounts, your passwords.\n\n"
    "WorldBaton can't tell whether a mod or file is safe. Its change notices and Host activity help you notice "
    "things; they don't stop them.\n\n"
    "Only create or join a group with people you would trust with full access to your computer, and with your "
    "server and world.")


def accept_risk(root) -> bool:
    """First-run agreement that can't be skipped. Nothing starts (so nothing syncs) until it's accepted."""
    if load_config().get("accepted_risk", 0) >= RISK_VERSION:
        return True
    root.title("WorldBaton: before you start")
    answer = tk.StringVar()
    root.protocol("WM_DELETE_WINDOW", lambda: answer.set("quit"))
    f = ttk.Frame(root, padding=20)
    f.pack(fill="both", expand=True)
    ttk.Label(f, text="Only use WorldBaton with people you fully trust", font=("Segoe UI", 14, "bold"),
              wraplength=500).pack(anchor="w")
    ttk.Label(f, text=RISK_TEXT, wraplength=500, justify="left").pack(anchor="w", pady=(10, 14))
    understood = tk.BooleanVar()
    row = ttk.Frame(f)
    go = ttk.Button(row, text="Continue", style="Accent.TButton", state="disabled", command=lambda: answer.set("ok"))
    ttk.Checkbutton(f, text="I understand, and I'll only use it with people I trust this much.", variable=understood,
                    command=lambda: go.config(state="normal" if understood.get() else "disabled")).pack(anchor="w")
    row.pack(anchor="e", pady=(14, 0))
    ttk.Button(row, text="Quit", command=lambda: answer.set("quit")).pack(side="left", padx=(0, 6))
    go.pack(side="left")
    root.wait_variable(answer)
    if answer.get() != "ok":
        root.destroy()
        return False
    f.destroy()
    cfg = load_config()
    cfg["accepted_risk"] = RISK_VERSION
    save_config(cfg)
    return True


def main():
    root = tk.Tk()
    sv_ttk.set_theme("dark", root)
    root.tk.call("configure_colors")  # Tk 9 doesn't fire the theme's <<ThemeChanged>> hook, leaving pages light
    dark_title_bar(root)
    try:
        root.iconphoto(True, tk.PhotoImage(file=ICON))  # True: every window and dialog uses it
    except tk.TclError:
        pass  # missing icon file: keep the default rather than fail to start
    if accept_risk(root):
        App(root, hidden="--tray" in sys.argv)
        root.mainloop()


if __name__ == "__main__":
    main()
