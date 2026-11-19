"""WorldBaton window (tkinter)."""
import ctypes
import json
import os
import secrets
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
from .instance import Instance
from .tray import Tray
from .syncthing import Syncthing

CONFIG = APP_DIR / "config.json"
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


def upgrade_config(cfg: dict) -> dict:
    """Versions before 0.4.0 had one group per PC and one tunnel address; now each group has its own."""
    if "group" in cfg:
        old = cfg.pop("group")
        old.setdefault("address", cfg.get("address", ""))
        cfg["groups"], cfg["active"] = [old], old["world_id"]
    cfg.setdefault("groups", [])
    return cfg


class App:
    def __init__(self, root: tk.Tk, hidden=False, instance: Instance | None = None):
        self.root, self.cfg, self.group, self.st = root, load_config(), None, None
        self.instance = instance  # tells us when WorldBaton is opened again
        self.cfg["settings"] = {**DEFAULT_SETTINGS, **self.cfg.get("settings", {})}
        upgrade_config(self.cfg)
        self.groups: list[core.Group] = []
        self.statuses = {}  # world_id -> that group's latest status()
        self.refused = {}  # wrong-password attempts to join this PC's groups, see core.admit
        self.busy = False
        self.notified_changes = self.activity_shown = None
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
        for info in self.cfg["groups"]:
            g = core.Group(self.st, info, APP_DIR)
            self.groups.append(g)
            try:
                self.st.set_ignores(g.world_id, core.IGNORES)  # groups made by older versions get new rules
            except Exception:
                pass
        self.st.set_name(core.with_proofs(self.cfg["player"], self.waiting_proofs()))
        self.group = next((g for g in self.groups if g.world_id == self.cfg.get("active")),
                          self.groups[0] if self.groups else None)
        self.show()
        self.tick()  # these two run for as long as WorldBaton does, for every group
        self.beat()
        self.check_updates()

    def waiting_proofs(self, but=None) -> list[str]:
        """Password proofs of the groups this PC is still waiting to be let into."""
        return [g.info["proof"] for g in self.groups if g.info.get("proof") and g is not but]

    def switch_to(self, g):
        self.group = g
        self.cfg["active"] = g.world_id if g else None
        save_config(self.cfg)
        self.notified_changes = self.activity_shown = None
        self.history_built = False
        self.show()

    def add_group(self, g):
        self.groups.append(g)
        self.cfg["groups"].append(g.info)
        self.switch_to(g)

    def address_for(self, g) -> str:
        """This PC's tunnel address for that group's server (each SquidServers server has its own tunnel)."""
        return g.info.get("address", "")

    def leave_group(self):
        g = self.group
        if not messagebox.askyesno("Leave group", f"Leave {g.info['name']} on this PC?\n\nWorldBaton stops syncing "
                                   f"it here. The server folder stays where it is:\n{g.world_path}\n\nTo come back "
                                   "later, ask for a new invite.", icon="warning"):
            return
        try:
            g.leave()
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))
            return
        self.groups.remove(g)
        self.cfg["groups"].remove(g.info)
        self.statuses.pop(g.world_id, None)
        self.switch_to(self.groups[0] if self.groups else None)

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
        ("p", "3. Pick a password, then send friends the group code and the password. Your WorldBaton lets them in "
              "on its own while it's open (the tray is enough)."),
        ("p", "4. To remove someone, press Remove a friend…. They can't come back, even with the password."),
        ("h", "Joining a friend's group"),
        ("p", "1. Press Join a friend's group, paste their group code and type the password."),
        ("p", "2. Your friend's WorldBaton lets you in as soon as it's open. Typo? Press Enter the password again…."),
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
        ("p", "• In more than one group? Press Add a group… at the top of My group, then switch between them there. "
              "Each group needs its own server folder (in SquidServers: its own server)."),
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

    def show(self, adding=False):
        self.clear()
        self.build_settings()
        self.refresh_history()
        if self.group and not adding:
            self.show_group()
        else:
            if self.groups:
                ttk.Button(self.body, text="← Back to my groups", command=self.show).pack(anchor="w", pady=(0, 8))
            ttk.Label(self.body, text=f"Hi {self.cfg['player']}!", font=("Segoe UI", 14, "bold")).pack(pady=(0, 8))
            ttk.Label(self.body, text="Share one Minecraft server with friends.\nWhoever is online can host it.",
                      justify="center").pack(pady=(0, 6))
            ttk.Label(self.body, text="An add-on for SquidServers: SquidServers runs the server, WorldBaton passes "
                                      "the world and the turn to host between you.", wraplength=420, justify="center",
                      foreground="#a0a0a0").pack(pady=(0, 16))
            ttk.Button(self.body, text="Create a group from my server", command=self.create_group).pack(fill="x", pady=4)
            ttk.Button(self.body, text="Join a friend's group", command=self.join_group).pack(fill="x", pady=4)
            if not self.cfg.get("has_squidservers") and not tools.squid_installed():
                hint = ttk.Frame(self.body)
                hint.pack(pady=(16, 0))
                ttk.Label(hint, text="WorldBaton couldn't find SquidServers on this PC. You can still join a group and "
                                     "get the world, but to host you'll need SquidServers.", wraplength=420,
                          justify="center", foreground="#ffd479").pack(pady=(0, 6))
                buttons = ttk.Frame(hint)
                buttons.pack()
                ttk.Button(buttons, text="Get SquidServers",
                           command=lambda: webbrowser.open("https://squidservers.com")).pack(side="left", padx=(0, 6))

                def have_it():  # e.g. a portable copy WorldBaton can't detect
                    self.cfg["has_squidservers"] = True
                    save_config(self.cfg)
                    hint.destroy()
                ttk.Button(buttons, text="I already have it", command=have_it).pack(side="left")

    def show_group(self):
        b = self.body
        top = ttk.Frame(b)
        top.pack(fill="x")
        if len(self.groups) > 1:
            names = [g.info["name"] for g in self.groups]
            pick = ttk.Combobox(top, values=names, state="readonly", width=28, font=("Segoe UI", 12, "bold"))
            pick.current(self.groups.index(self.group))
            pick.bind("<<ComboboxSelected>>", lambda _: self.switch_to(self.groups[pick.current()]))
            pick.pack(side="left")
        else:
            ttk.Label(top, text=self.group.info["name"], font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Add a group…", command=lambda: self.show(adding=True)).pack(side="right")
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
        if self.group.info.get("proof"):
            ttk.Button(row2, text="Enter the password again…", command=self.retry_password).pack(side="left", padx=(0, 6))
        else:
            ttk.Button(row2, text="Invite a friend", command=self.invite).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Open server folder", command=lambda: os.startfile(self.group.world_path)).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Backup folder…", command=self.pick_backup).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Change server folder…", command=self.change_folder).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="My address…", command=self.ask_address).pack(side="left")
        self.backup_label = ttk.Label(b, wraplength=440, foreground="#a0a0a0")
        self.backup_label.pack(anchor="w", pady=(8, 0))
        last = ttk.Frame(b)
        last.pack(fill="x", pady=(6, 0))
        if self.group.is_owner():
            ttk.Button(last, text="Remove a friend…", command=self.remove_friend).pack(side="left", padx=(0, 6))
        ttk.Button(last, text="Leave this group…", command=self.leave_group).pack(side="left")
        self.show_backup()
        if self.group.world_id in self.statuses:
            self.render(self.statuses[self.group.world_id])  # don't wait for the next refresh after switching

    # --- refresh -----------------------------------------------------------------
    def tick(self):
        """Every few seconds: keep every group synced and watched, then refresh the group on screen."""
        if not self.busy:  # never reconcile while a restore/backup is touching a server folder
            for g in list(self.groups):
                self.watch_group(g)
            self.admit_friends()
            s = self.statuses.get(self.group.world_id) if self.group else None
            if s and getattr(self, "headline", None) and self.headline.winfo_exists():
                self.render(s)
        self.root.after(REFRESH_MS, self.tick)

    def watch_group(self, g):
        try:
            g.reconcile()
            s = g.status()
        except Exception as e:
            if g is self.group and getattr(self, "headline", None) and self.headline.winfo_exists():
                self.reason.config(text=f"Sync engine busy ({e}). Retrying…")
            return
        player, name = self.cfg["player"], g.info["name"]
        if g.info.get("proof") and s["baton"]:  # the owner let this PC in
            try:
                g.joined(player, self.waiting_proofs(but=g))
                save_config(self.cfg)
                self.notify(f"You're in {name}", "The world is copying to this PC.")
                if g is self.group:
                    self.show()
            except Exception:
                pass
        if s["me_hosting"]:
            try:  # every refresh, so there's little time to edit the log before it's read
                g.record_activity(player)
                g.watch_offline_edits(player)
                if g.update_server_up() == "up":
                    self.post_discord(g, f"🟢 **{player}** is hosting **{name}**. "
                                         f"Join at `{self.address_for(g) or 'ask them for the address'}`")
            except Exception:
                pass
        for title, message in core.events(self.statuses.get(g.world_id), s, self.st.my_id):
            self.notify(f"{name}: {title}" if len(self.groups) > 1 else title, message)
        self.statuses[g.world_id] = s

    def admit_friends(self):
        try:
            let_in, wrong = core.admit(self.st, self.groups, self.refused)
        except Exception:
            return  # sync engine busy: next refresh
        for g, name in let_in:
            self.notify(f"{name} joined {g.info['name']}", "Their copy of the world is starting to sync.")
        for name in wrong:
            self.notify("Wrong group password", f"{name} tried to join with a wrong password.")
            if len(self.refused) == core.GUESSES_PER_HOUR:
                self.notify("Joining paused for an hour", "Too many wrong passwords. Nobody can join your groups "
                            "for an hour. Change the password if you didn't expect this.")

    def render(self, s):
        """Show one group's status on the My group tab."""
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
            warns.append("Your server is running but it isn't your turn! Stop it: what it changes will be undone. "
                         "Press Host now first next time.")
        self.show_changes(s)
        self.show_backup()
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
        if not self.busy:
            for g in list(self.groups):
                try:
                    g.heartbeat()
                except Exception:
                    pass
        self.root.after(core.HEARTBEAT_EVERY * 1000, self.beat)

    # --- actions -----------------------------------------------------------------
    def create_group(self):
        start = tools.squid_servers_dir() if tools.squid_servers_dir().is_dir() else Path.home()
        path = filedialog.askdirectory(title="Pick your server folder", initialdir=start, mustexist=True)
        if not path:
            return
        name = simpledialog.askstring("WorldBaton", "Group name:", initialvalue=Path(path).name, parent=self.root)
        if not name:
            return
        try:
            g = core.Group.create(self.st, APP_DIR, name.strip(), Path(path), self.cfg["player"],
                                  taken=[x.world_path for x in self.groups])
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))
            return
        self.add_group(g)
        if self.ask_password(g):
            self.invite()

    def join_group(self):
        code = simpledialog.askstring("WorldBaton", "Paste the group code your friend sent you:", parent=self.root)
        if not code:
            return
        try:
            invite = core.decode_invite(code)
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))
            return
        password = simpledialog.askstring("WorldBaton", f"Type the password for {invite['g']}:", parent=self.root)
        if not password:
            return
        try:
            squid = tools.squid_servers_dir()
            parent = squid if squid.is_dir() else Path.home() / "WorldBaton servers"
            g = core.Group.join(self.st, APP_DIR, invite, parent, password.strip(), self.cfg["player"],
                                joined=[x.world_id for x in self.groups], waiting=self.waiting_proofs())
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))
            return
        self.add_group(g)
        messagebox.showinfo("WorldBaton", f"Asked {invite['on']} to let you in. That happens on its own as soon as "
                                          f"{invite['on']}'s WorldBaton is open, and the world starts copying.")

    def retry_password(self):
        g = self.group
        password = simpledialog.askstring("WorldBaton", f"Type the password for {g.info['name']} again:", parent=self.root)
        if not password:
            return
        try:
            g.set_join_password(password.strip(), self.cfg["player"], self.waiting_proofs(but=g))
            save_config(self.cfg)
        except Exception as e:
            messagebox.showerror("WorldBaton", str(e))

    def ask_password(self, g, again=False) -> bool:
        """The owner picks the password friends type to join. False if cancelled."""
        suggestion = g.info.get("password") or secrets.token_hex(4)
        while True:
            pw = simpledialog.askstring(
                "Group password", f"{'Pick a new' if again else 'Pick a'} password friends type to join {g.info['name']} "
                f"(at least {core.MIN_PASSWORD} characters).\nShare it only with people you trust with your PC."
                + ("\nFriends already in the group stay in." if again else ""),
                initialvalue="" if again else suggestion, parent=self.root)
            if pw is None:
                return False
            try:
                g.set_password(pw.strip())
            except ValueError as e:
                messagebox.showerror("WorldBaton", str(e))
                continue
            save_config(self.cfg)
            return True

    def invite(self):
        g = self.group
        if not g.is_owner():
            owner = core.plain_name(self.st.devices().get(g.info["owner"], "")) or g.info.get("owner_name", "the owner")
            self.code_dialog(f"Send this group code to your friend. Only {owner} knows the password: ask them "
                                   f"for it. {owner}'s WorldBaton is what lets people in.", g.invite_code(owner))
            return
        if not g.info.get("key") and not self.ask_password(g):
            return  # a group made before passwords
        win = self.code_dialog("Send your friend the group code and the password. Anyone with both can join while "
                               "your WorldBaton is open (the tray is enough).", g.invite_code(self.cfg["player"]))
        row = ttk.Frame(win)
        row.pack(fill="x", padx=12, pady=(12, 12))
        ttk.Label(row, text=f"Password:  {g.info['password']}", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(row, text="Copy", command=lambda: self.copy(g.info["password"])).pack(side="left", padx=6)

        def change():
            win.destroy()
            if self.ask_password(g, again=True):
                self.invite()
        ttk.Button(row, text="Change password…", command=change).pack(side="left")

    def remove_friend(self):
        g = self.group
        members = (self.statuses.get(g.world_id) or {}).get("members") or []
        if not members:
            messagebox.showinfo("WorldBaton", "There's nobody to remove yet.")
            return
        win = tk.Toplevel(self.root)
        win.title("Remove a friend")
        ttk.Label(win, text=f"Who should leave {g.info['name']}?").pack(anchor="w", padx=12, pady=(12, 4))
        pick = ttk.Combobox(win, values=[m["name"] for m in members], state="readonly", width=30)
        pick.current(0)
        pick.pack(padx=12, fill="x")

        def remove():
            m = members[pick.current()]
            baton = g.read_baton() or {}
            hosting = (f"\n\n{m['name']} is hosting right now: that session is lost, and after 5 minutes someone else "
                       "can host." if baton.get("holder") == m["id"] else "")
            if not messagebox.askyesno("Remove a friend", f"Remove {m['name']} from {g.info['name']}?\n\nTheir PC stops "
                                       "syncing with everyone and can't come back, even with the password. They keep "
                                       f"the copy of the world they already have.{hosting}", icon="warning", parent=win):
                return
            try:
                g.remove_member(m["id"])
                save_config(self.cfg)
            except Exception as e:
                messagebox.showerror("WorldBaton", str(e), parent=win)
                return
            win.destroy()
            if messagebox.askyesno("WorldBaton", f"{m['name']} is out. Change the group password too, so someone "
                                   "they told can't join?"):
                self.ask_password(g, again=True)
            if g.cloud:
                messagebox.showinfo("WorldBaton", f"{m['name']} still has the cloud copy's keys. To lock them out of it "
                                    "too, make a new key with your storage provider and save it in Settings.")

        buttons = ttk.Frame(win)
        buttons.pack(pady=12)
        ttk.Button(buttons, text="Remove", command=remove).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="left")
        self.popup(win)

    def ask_address(self) -> bool:
        """Ask for this PC's tunnel address and remember it. False if cancelled."""
        msg = ("Paste your server address from SquidServers (the tunnel address friends join with).\n"
               "WorldBaton remembers it. You can leave it empty and tell friends yourself.")
        addr = simpledialog.askstring(f"My address for {self.group.info['name']}", msg,
                                      initialvalue=self.address_for(self.group), parent=self.root)
        if addr is None:
            return False
        self.group.info["address"] = addr.strip()
        save_config(self.cfg)
        baton = self.group.read_baton()
        if baton and baton.get("holder") == self.st.my_id:  # hosting now: show friends the new address
            self.group.write_baton({**baton, "address": self.group.info["address"]})
        return True

    def host(self):
        problems = tools.prehost_problems(self.group.world_path)
        if problems and not messagebox.askyesno("Before you host", "\n\n".join(problems) + "\n\nHost anyway?",
                                                icon="warning"):
            return
        if "address" not in self.group.info and not self.ask_address():
            return
        addr = self.address_for(self.group)
        self.run_busy("Getting the world ready…", lambda: self.group.start_hosting(self.cfg["player"], addr),
                      lambda _: messagebox.showinfo("WorldBaton", "You're the host. Start the server in SquidServers now.\n"
                                                    "When you're done: stop the server there, then press 'Stop hosting' here."))

    def stop(self):
        def done(warning):
            if warning:
                messagebox.showwarning("WorldBaton", warning)
            tip = ("It's also in the cloud copy, so friends can get it even after you turn off your PC."
                   if self.group.cloud and not warning else
                   "If you use a cloud drive as the backup folder, wait until it says it's up to date before turning off your PC."
                   if self.group.backup_dir else "")
            messagebox.showinfo("WorldBaton", "Done! Keep WorldBaton open until your friends show 100% synced.\n" + tip)
            self.post_discord(self.group, f"⚪ **{self.cfg['player']}** stopped hosting **{self.group.info['name']}**. "
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
        start = tools.squid_servers_dir() if tools.squid_servers_dir().is_dir() else Path.home()
        path = filedialog.askdirectory(title="Pick the server folder SquidServers made", initialdir=start, mustexist=True)
        if not path:
            return

        def done(_):
            save_config(self.cfg)
            messagebox.showinfo("WorldBaton", f"Done! The group's world now lives in:\n{path}\n\n"
                                "You can start it from SquidServers when it's your turn to host.")
        others = [x.world_path for x in self.groups if x is not self.group]
        self.run_busy("Copying the group's files…", lambda: self.group.move_world(Path(path), taken=others), done)

    def show_backup(self):
        d, cloud = self.group.backup_dir, self.group.cloud
        if cloud:
            newest = max(cloud.zips, default=None)
            state = (self.group.transfer or (f"couldn't reach it: {cloud.error}" if cloud.error else
                     f"has version {newest}" if newest else "empty until the next Stop hosting"))
            text = f"Cloud copy ({cloud.s3.bucket}): {state}"
        else:
            text = (f"Backup folder: {d}" if d else
                    "No cloud copy or backup folder. Only needed if your group is rarely online at the same time.")
        self.backup_label.config(text=text)

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
                moving = self.group and self.group.transfer
                self.reason.config(text=f"{text} {moving}" if moving else text)
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
        """Handle tray menu clicks, and WorldBaton being opened again, here on tkinter's thread."""
        while self.instance and not self.instance.shows.empty():
            self.instance.shows.get()
            self.tray.clicks.put("open")
        while not self.tray.clicks.empty():
            click = self.tray.clicks.get()
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            if click == "quit":
                self.quit()
                return
        self.root.after(300, self.poll_tray)

    def post_discord(self, group, text):
        url = group.group_settings().get("discord_webhook")
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

        ttk.Separator(f).pack(fill="x", pady=(12, 0))
        ttk.Label(f, text=f"For {self.group.info['name']}", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(6, 0))
        heading("World backups")
        keep = tk.BooleanVar(value=self.group.keep_local > 0)

        def set_keep():
            self.group.info["local_backups"] = core.KEEP_LOCAL if keep.get() else 0
            save_config(self.cfg)
        ttk.Checkbutton(f, text=f"Keep the last {core.KEEP_LOCAL} worlds I host, so they can be restored", variable=keep,
                        command=set_keep).pack(anchor="w")
        note("Saved when you press Stop hosting. Each one is about the size of the server folder. Restore one from "
             "the History tab.")

        heading("Cloud copy (whole group)")
        cfg = self.group.group_settings().get("cloud") or {}
        fields = {}
        grid = ttk.Frame(f)
        grid.pack(anchor="w", fill="x")
        for row_no, (key, label, hide) in enumerate((("endpoint", "S3 endpoint", False), ("bucket", "Bucket", False),
                                                     ("key_id", "Access key ID", False),
                                                     ("secret", "Secret access key", True))):
            ttk.Label(grid, text=label).grid(row=row_no, column=0, sticky="w", padx=(0, 8), pady=1)
            fields[key] = tk.StringVar(value=cfg.get(key, ""))
            ttk.Entry(grid, textvariable=fields[key], width=44, show="•" if hide else "").grid(row=row_no, column=1,
                                                                                               sticky="we", pady=1)
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(4, 0))

        def save_cloud(turn_off=False):
            new = None if turn_off else {k: v.get().strip() for k, v in fields.items()}
            group = self.group

            def done(_):
                save_config(self.cfg)
                self.show_backup()
                messagebox.showinfo("WorldBaton", "The cloud copy is off. Its files stay in your bucket." if turn_off else
                                    "It works! Saved for the whole group. Each Stop hosting now uploads the world, and "
                                    "anyone in the group can get it from there.")
            self.tabs.select(self.body)
            self.run_busy("Checking the cloud copy…" if new else "Turning off the cloud copy…",
                          lambda: group.set_cloud(new), done)
        ttk.Button(row, text="Check and save", command=save_cloud).pack(side="left", padx=(0, 6))
        if cfg:
            ttk.Button(row, text="Turn off", command=lambda: save_cloud(turn_off=True)).pack(side="left")
        note("Free S3 storage such as Cloudflare R2 or Backblaze B2 (10 GB free each). Each Stop hosting uploads the "
             "world, so friends can host even when nobody who has it is online. Anyone in the group gets these keys.")

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
        rows = [(e.get("end", 0), e) for e in sessions] + [(b["mtime"], {"version": v})
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

    def hosting(self) -> list[str]:
        """Names of the groups this PC is hosting right now."""
        return [g.info["name"] for g in self.groups if self.statuses.get(g.world_id, {}).get("me_hosting")]

    def quit(self):
        unsynced = [g.info["name"] for g in self.groups
                    if (s := self.statuses.get(g.world_id)) and s["baton"] and s["baton"].get("last_host") == self.st.my_id
                    and any(m["online"] and (m["synced"] or 0) < 100 for m in s["members"])]
        if self.busy:
            if not messagebox.askyesno("WorldBaton", "WorldBaton is still working (" + (self.reason.cget("text") or
                                       "busy") + "). Close anyway?"):
                return
        elif self.hosting():
            if not messagebox.askyesno("WorldBaton", f"You're hosting {', '.join(self.hosting())}. If you close "
                                       "WorldBaton, friends can take over after 5 minutes and your session won't be "
                                       "shared. Close anyway?"):
                return
        elif unsynced:
            if not messagebox.askyesno("WorldBaton", f"Your friends in {', '.join(unsynced)} don't have your latest "
                                       "world yet. Close anyway?"):
                return
        self.shutdown()

    def shutdown(self):
        if self.instance:
            self.instance.stop()  # first, so a restarting WorldBaton (after an update) can take over
        self.tray.stop()
        if self.st:
            for g in self.groups:
                try:
                    g.park()
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
        if self.hosting():
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
    instance, hidden = Instance(), "--tray" in sys.argv
    deadline = time.time() + 30
    while not instance.claim(wait=0.5):  # another WorldBaton is running, starting or closing
        if hidden or instance.show_other():
            return  # signing in to Windows: it's already in the tray. Otherwise it's now in front.
        if time.time() > deadline:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("WorldBaton", "WorldBaton is already running. Look for its icon in the tray, "
                                "next to the clock.")
            return
    instance.listen()
    if tools.can_autostart() and tools.autostart_enabled():
        try:
            tools.set_autostart(True)  # keep Start with Windows pointing at this .exe, even if it was moved
        except OSError:
            pass
    root = tk.Tk()
    sv_ttk.set_theme("dark", root)
    root.tk.call("configure_colors")  # Tk 9 doesn't fire the theme's <<ThemeChanged>> hook, leaving pages light
    dark_title_bar(root)
    try:
        root.iconphoto(True, tk.PhotoImage(file=ICON))  # True: every window and dialog uses it
    except tk.TclError:
        pass  # missing icon file: keep the default rather than fail to start
    try:
        if accept_risk(root):
            App(root, hidden=hidden, instance=instance)
            root.mainloop()
    finally:
        instance.stop()

if __name__ == "__main__":
    main()
