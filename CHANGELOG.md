# Changelog

What changed in each version, in plain words. WorldBaton shows this in its *What's new* tab, after each
update, and as the notes of each GitHub release. Every release needs a section here (the release build checks).

## 0.6.2
- Fixed being stuck on "Your copy is out of date" when only server.properties or eula.txt was missing. SquidServers
  rewrites those by itself, and WorldBaton then waited for the last host to come online to put them back.
- Fixed the midnight "The server log was edited or cut while hosting" warning. When SquidServers has the log open,
  Minecraft empties the same file instead of starting a new one; WorldBaton now handles that too.

## 0.6.1
- Fixed a false "The server log was replaced while the server kept running" warning at midnight. Minecraft starts a
  new log each day while the server runs; WorldBaton now reads the end of the old one, so commands typed just
  before midnight still show up in Host activity.

## 0.6.0
- Cloud copy: put the world in free S3 storage such as Cloudflare R2 or Backblaze B2 (Settings → Cloud copy,
  set once for the whole group). Each Stop hosting uploads it, so anyone in the group can get the newest world
  and host, even when nobody who has it is online. Earlier sessions there can be restored from History.

## 0.5.1
- Opening WorldBaton while it's already running (even hidden in the tray) now brings that window to the front
  instead of starting a second copy. Start with Windows doesn't open a second one either, and it keeps
  working if you move WorldBaton.exe.
- Fixed a false "The server log was edited or cut while hosting" warning that could show up every time the host
  started the server, because Windows gives the new log file the old one's creation date.

## 0.5.0
- Joining is easier: the owner picks a group password, then sends friends the group code and the password. Friends
  paste both and are let in on their own while the owner's WorldBaton is open (the tray is enough). No more
  sending a code back. Typed it wrong? Press "Enter the password again".
- The owner can remove a friend from a group (Remove a friend...). Everyone's PC stops syncing with theirs, and
  they can't come back, even with the password. They keep the copy of the world they already have.
- After 10 wrong passwords in an hour, nobody can join for an hour, and the owner is told.
- Starting your server when it isn't your turn is now caught while WorldBaton is open: syncing pauses for that
  server, so its half-saved world never reaches friends and the newer world never lands under it. Once it
  stops, what it changed is undone (before, the last host's changes were sent to everyone).
- Host now needs the server stopped first (start it once you're the host), so every admin command of a session
  is in Host activity, including ones typed before pressing Host now.
- Groups you're already in keep working as they are. Owners of older groups pick a password the next time they
  press Invite a friend. Group codes from older versions don't work anymore: ask for a new one.

## 0.4.0
- Multiple groups: be in several groups at once and switch between them at the top of My group. Every group
  keeps syncing in the background, each has its own tunnel address, and you can leave a group (its
  files stay on your PC).
- Notifications: get told when someone starts hosting, when their server is up (with the address), when it's
  your turn, and when the server changes. Turn them off in the new Settings tab.
- WorldBaton can keep running in the tray when you close the window, and start with Windows, so friends can get
  the world from you even when you didn't open it.
- Ask to host next: while someone hosts, press "Ask to host next". They're told, and you're told when it's
  your turn.
- History tab: every session, who hosted and for how long. Your PC keeps the last 2 worlds you hosted, and
  any version with a backup can be brought back for everyone.
- Update my game's mods: copies the server's mods into your own Minecraft (normal launcher, CurseForge, Prism,
  Modrinth...). Mods you added yourself stay.
- Discord: paste a channel webhook in Settings and WorldBaton posts when someone's server is up and when they stop.
- Before you host, WorldBaton checks for a nearly full drive and for a SquidServers server that was set up with a
  different loader version than the group's.
- Clearer that WorldBaton is an add-on for SquidServers, not a replacement: SquidServers runs the server, and
  WorldBaton passes the world and the turn to host. If SquidServers isn't on your PC, WorldBaton says so
  (it finds SquidServers wherever it's installed, and its server folder even if you moved it).
- New look: WorldBaton has its own logo, on the app, the taskbar and the window.
- What's new: this list now lives in its own tab, and WorldBaton shows what changed once after each update.
- Update prompts now say what the new version changes before you decide.
- Pop-up windows (codes, What's new) open over WorldBaton and have dark title bars too.

## 0.3.1
- The first time you open WorldBaton, it explains the risk of sharing a server folder: anyone in your group can
  put mods and files on your PC. You have to accept before anything starts syncing.

## 0.3.0
- Dark theme with a dark title bar.
- "Changed since you last looked": see when mods, operators, datapacks, important server settings or logging
  gamerules change, with a button to open the server's mods folder.
- Host activity: admin commands (op, gamemode, give, tp, ban...) used while someone hosts are shared with the
  whole group.
- Signs that someone tried to hide commands are shown in red: an edited server log, files changed while the
  server was off, mods loaded from outside the mods folder, or WorldBaton closed mid-session.

## 0.2.0
- Setup guide tab that walks you through your first group.
- "Check for updates" button.

## 0.1.0
- First release: take turns hosting one Minecraft server, with the world synced PC to PC.
- Only one person can host at a time, and you can only host with the newest world.
- Optional backup folder for groups that are rarely online together.
- Change server folder, so friends can host from their own SquidServers server.
- Remembers your tunnel address, and updates itself.
