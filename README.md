# WorldBaton

Share one Minecraft server with your friends. Whoever is online can host it.

WorldBaton keeps a copy of your server folder on every friend's PC and passes the
"baton" (the right to host) between you, so only one person runs the world at a time
and nobody loses progress. No cloud, no accounts, no monthly cost: your PCs sync
directly with each other using [Syncthing](https://syncthing.net).

Works with [SquidServers](https://squidservers.com) server folders and plain server
folders. **WorldBaton is not made by or affiliated with SquidServers / Aqura LLC**, and
it doesn't modify their app: it only syncs your server folder.

## Download

Get `WorldBaton.exe` from the [latest release](https://github.com/Potrosho/worldbaton/releases/latest).
Windows may warn that it's from an unknown publisher: click *More info → Run anyway*.
WorldBaton checks for updates each time it opens and offers to install them.

## How to use

1. **Owner:** open WorldBaton → *Create a group from my server* → pick your server folder
   (SquidServers keeps them in `%LOCALAPPDATA%\SquidServers`).
2. **Owner:** *Invite a friend* → send them the invite code.
3. **Friend:** *Join a friend's group* → paste the code → send back the join code it shows.
4. **Owner:** paste the join code → the world starts copying to your friend.
5. **To play:** press *Host now*, paste your SquidServers tunnel address, start the server in
   SquidServers. When done: stop the server there, then press *Stop hosting* and keep
   WorldBaton open until friends show 100% synced.

Friends see who is hosting and the address to join with.

### Never online at the same time? Add a backup folder

Normally the world goes straight from PC to PC, so two PCs need to be on together.
If your group rarely overlaps, share a folder in Google Drive, OneDrive or Dropbox,
install that drive's desktop app, and in WorldBaton press *Backup folder…* and pick it
(everyone picks their own copy of the same shared folder).

Every time someone presses *Stop hosting*, WorldBaton saves a zip of the world there
(the newest 3 are kept). When you want to host and your copy is behind, it loads the
newest zip first. Who is hosting is also shown through that folder, so it works even if
you never see each other online. Before turning off your PC, wait until the drive app
says it's up to date.

If your PC crashes while hosting, reopen WorldBaton: as long as nobody took over, press
*Stop hosting* to share what you played.

## How it works

- Each group is two Syncthing folders: the **world** (your server folder) and a tiny
  **baton** file (who is hosting, their address, a heartbeat, the world version).
- Only the last host sends world changes; everyone else's copy is receive-only, so stray
  edits on a friend's PC are undone instead of spread.
- While you host, your world folder is paused: friends never get half-saved files. Your
  session is shared once you press *Stop hosting*.
- You can only host when your copy matches the newest version. If the host's PC dies,
  friends can take over after 5 minutes without a heartbeat (that session is lost).
- Mods and operators: when the server's `mods` folder or `ops.json` changes, everyone sees what changed
  (install the same mods in your own Minecraft to keep joining).
- Host activity: while you host, admin commands from the server log (op, gamemode, give, tp, ban…) are
  shared with the group. It catches casual abuse, not a determined cheater: the host controls their own
  PC and could turn logging off or edit files. Operator changes are seen either way, since `ops.json` syncs.
- Logs, crash reports, caches and SquidServers' per-PC files (`squidservers/`,
  `.mc_server_state.json`) are not synced. Each person's tunnel stays their own.

## Limits (for now)

- Windows only, one group per PC.
- Without a backup folder, two PCs must be online at the same time to pass the world on.
- WorldBaton can't tell when a cloud drive has finished uploading; the host has to check.
- Anyone in the group can host and change the world, so only invite people you trust.
  Syncthing keeps the last 3 versions of changed files in `.stversions` for recovery.
- To host from SquidServers, a friend first creates a server there with the same type and
  version (and a different name), then picks its folder with *Change server folder…*.
  WorldBaton fills it with the group's world, mods and config and keeps that PC's own
  start scripts and RAM setting.

## Development

```bash
pip install sv-ttk
```

```bash
python scripts/get_syncthing.py
```

```bash
python -m worldbaton
```

```bash
python -m unittest discover -s tests -v
```

Build `dist\WorldBaton.exe` (needs `pip install pyinstaller`):

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

### Publishing a release

1. Change `__version__` in `worldbaton/__init__.py` (e.g. `0.2.0`) and commit.
2. Tag and push:

```bash
git tag v0.2.0
```

```bash
git push origin main --tags
```

GitHub Actions tests, builds and publishes `WorldBaton.exe` as release `v0.2.0`.
Everyone's WorldBaton offers it as an update the next time they open it.

## License

WorldBaton is licensed under the [GPL-3.0](LICENSE).
Syncthing is © The Syncthing Authors, MPL-2.0; its license ships with the app.
