"""WorldBaton window (tkinter)."""
import ctypes
import json
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import sv_ttk

from . import __version__, core, updater
from .syncthing import Syncthing

APP_DIR = Path(os.environ.get("WORLDBATON_HOME") or Path(os.environ.get("APPDATA", Path.home())) / "WorldBaton")
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
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root, self.cfg, self.group, self.st = root, load_config(), None, None
        self.busy = False
        root.title(f"WorldBaton {__version__}")
        root.minsize(520, 460)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True)
        self.body = ttk.Frame(self.tabs, padding=16)
        self.guide = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(self.body, text="My group")
        self.tabs.add(self.guide, text="Setup guide")
        self.build_guide()
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
            self.root.destroy()
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
        ("h", "Mods and fair play"),
        ("p", "• When the host adds or removes a mod, everyone sees it under My group. Put the same mod in your own "
              "Minecraft's mods folder too, or you can't join."),
        ("p", "• Admin commands (op, gamemode, give, tp…) used on the server are listed under Host activity for the whole "
              "group, and so are changes to who is an operator."),
        ("h", "Rarely online at the same time?"),
        ("p", "• Share a Google Drive, OneDrive or Dropbox folder with the group and install that drive's desktop app."),
        ("p", "• Press Backup folder… and pick your copy of that shared folder. Everyone does this on their own PC."),
        ("p", "• After Stop hosting, wait until the drive app says it's up to date before turning off your PC."),
    ]

    def build_guide(self):
        g = self.guide
        text = tk.Text(g, wrap="word", relief="flat", padx=4, pady=4, height=18,
                       background=self.root.cget("background"), font=("Segoe UI", 10))
        bar = ttk.Scrollbar(g, command=text.yview)
        text.config(yscrollcommand=bar.set)
        text.tag_config("h", font=("Segoe UI", 11, "bold"), spacing1=10, spacing3=4)
        text.tag_config("p", lmargin1=4, lmargin2=18, spacing3=3)
        for tag, line in self.GUIDE:
            text.insert("end", line + "\n", tag)
        text.config(state="disabled")
        foot = ttk.Frame(g)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Label(foot, text=f"WorldBaton {__version__}", foreground="#a0a0a0").pack(side="left")
        ttk.Button(foot, text="Check for updates", command=lambda: self.check_updates(manual=True)).pack(side="right")
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

    def clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def show(self):
        self.clear()
        if self.group:
            self.show_group()
        else:
            ttk.Label(self.body, text=f"Hi {self.cfg['player']}!", font=("Segoe UI", 14, "bold")).pack(pady=(0, 8))
            ttk.Label(self.body, text="Share one Minecraft server with friends.\nWhoever is online can host it.",
                      justify="center").pack(pady=(0, 16))
            ttk.Button(self.body, text="Create a group from my server", command=self.create_group).pack(fill="x", pady=4)
            ttk.Button(self.body, text="Join a friend's group", command=self.join_group).pack(fill="x", pady=4)

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
        for w in (self.host_btn, self.stop_btn, self.discard_btn):
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
        self.reason.config(text="" if s["me_hosting"] else s["reason"] +
                           (f" ({s['need_bytes'] / 1e6:.0f} MB left)" if s["need_bytes"] else ""))

        warns = []
        if s["lost_baton"]:
            warns.append("Someone else took over while you were hosting. Your session is kept on this PC only.")
        if s["conflicts"]:
            warns.append("Two people changed the baton at the same time. Check who is really hosting before playing.")
        if s["server_running"] and not s["me_hosting"]:
            warns.append("Your server is running but you aren't the host! Stop it, or your changes will be lost.")
        self.warning.config(text="\n".join(warns))

        self.members.config(state="normal")
        self.members.delete("1.0", "end")
        for m in s["members"] or []:
            sync = "" if m["synced"] is None else f", {m['synced']:.0f}% synced"
            self.members.insert("end", f"{'●' if m['online'] else '○'} {m['name']} — {'online' if m['online'] else 'offline'}{sync}\n")
        if not s["members"]:
            self.members.insert("end", "No friends yet. Press 'Invite a friend'.")
        self.members.config(state="disabled")
        self.show_changes(s)
        self.show_activity()

        self.host_btn.config(state="normal" if s["can_host"] else "disabled")
        self.stop_btn.config(state="normal" if s["me_hosting"] else "disabled")
        self.discard_btn.config(state="normal" if s["lost_baton"] else "disabled")
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
        if not changes:
            self.changes.pack_forget()
            self.changes_row.pack_forget()
            return
        more = f"\n…and {len(changes) - 8} more" if len(changes) > 8 else ""
        self.changes.config(text="Changed since you last looked:\n" + "\n".join(changes[:8]) + more +
                            ("\nAdd/remove the same mods in your own Minecraft to keep joining."
                             if any("mod" in c for c in changes) else ""))
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
        self.activity_shown = entries
        self.activity.config(state="normal")
        self.activity.delete("1.0", "end")
        for e in entries:
            when = time.strftime("%d %b %H:%M", time.localtime(e.get("t", 0)))
            self.activity.insert("end", f"{when}  hosted by {e.get('host', '?')} — {e.get('text', '')}\n")
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

    # --- helpers -----------------------------------------------------------------
    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def code_dialog(self, label, code):
        win = tk.Toplevel(self.root)
        win.title("WorldBaton")
        dark_title_bar(win)
        ttk.Label(win, text=label, wraplength=420).pack(anchor="w", padx=12, pady=(12, 4))
        e = ttk.Entry(win, width=60)
        e.insert(0, code)
        e.config(state="readonly")
        e.pack(padx=12, fill="x")
        ttk.Button(win, text="Copy", command=lambda: self.copy(code)).pack(pady=(6, 0))
        return win

    def on_close(self):
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

    def offer_update(self, tag, url):
        s = getattr(self, "last_status", None)
        if s and s["me_hosting"]:
            messagebox.showinfo("WorldBaton", f"WorldBaton {tag} is out. It'll offer to update next time you open it "
                                "when you're not hosting.")
            return
        if not messagebox.askyesno("WorldBaton", f"WorldBaton {tag} is out (you have {__version__}).\n"
                                   "Update now? WorldBaton will restart."):
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


def main():
    root = tk.Tk()
    sv_ttk.set_theme("dark", root)
    root.tk.call("configure_colors")  # Tk 9 doesn't fire the theme's <<ThemeChanged>> hook, leaving pages light
    dark_title_bar(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
