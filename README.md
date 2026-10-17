# 99 Companion

A companion for the Asheron's Call 1999 client: a small relay the client connects through, and a local
dashboard that reads the game traffic to track your character, spells, components, experience and
surroundings. Windows.

## Quick start

1. On GitHub, **Code › Download ZIP**, and unzip it anywhere (your Documents folder is fine).
2. Double-click **`start.cmd`**. The first time, it sets up its own Python inside the folder (a one-time
   download of about 40 MB: no administrator rights, nothing installed on the system). A window opens (leave
   it open while you play: closing it stops the companion) and the dashboard opens in your browser.
3. In the dashboard's **Settings › Accounts**, add your account (name and password). Its **▶** button, in the
   header, starts the game logged in, through the companion. (The game folder is found on its own; if it
   isn't, Settings › Game says so and lets you pick it.)

Or start the game your usual way, pointed at host `127.0.0.1`, port `9110`.

Next time, just double-click `start.cmd`. Updates come from GitHub on their own (see Updates).

The relay forwards every packet unchanged before decoding a copy, so the server sees exactly the traffic it
would without it (and so does your game, unless you turn on an experimental feature: see below). The companion never plays for you: everything in game is done by you; the dashboard shows,
plans and guides.

## Architecture

```
client ⇄ relay ⇄ server        python -m companion.relay   long-lived, tiny, rarely changes
           │
       journals/session-*.cap  (every datagram, password redacted)
           │  TCP tap 127.0.0.1:9119
           ▼
       companion               python -m companion          decoder + tracker + dashboard
```

- **Multiple characters.** Every client connects to the same `127.0.0.1:9110`; the relay tells them apart by
  source address and gives each its own upstream socket (the server identifies players by address), journal
  and session number. The dashboard shows one tab per character.
- The **relay** only forwards UDP, journals sessions and serves the tap. It never waits on a consumer; one
  that falls behind is disconnected and reconnects. A client silent for 2 minutes has quit: its session ends
  (the tab shows "logged out") and is dropped 15 minutes later.
- A **companion** that connects is replayed every active session from its start, then streamed live, so it
  can be stopped, updated and restarted mid-game and comes back with full state.
- The relay survives a quick restart: upstream ports come from a fixed pool (`--upstream-ports`, default
  9120-9139, up to 20 clients), and each session's client address, port and journal are recorded in
  `journals/current.json`, which a restarted relay rebinds.

## Run

`start.cmd` runs everything in one window and opens the dashboard (http://127.0.0.1:8099/) in your browser.
The window (`python -m companion.reloader`) runs the relay and the dashboard and keeps both going: the
dashboard restarts when its code changes (an edit or an update), and the relay when a file it runs changes,
when its settings change, or if it stops. Closing the window stops both; game clients keep running.

`start.cmd` is the only thing to run. The rest of the scripts are in `tools\`, for when you want a part on its own:

```
tools\companion.cmd      start.cmd without opening the browser (--no-relay: leave the relay to tools\relay.cmd)
tools\relay.cmd          just the relay (python -m companion.relay); start.cmd's window leaves a relay it didn't start alone
tools\relay-tint.cmd     the same with the experimental vendor backgrounds on (--tint-store)
tools\relay-stop.cmd     stop a relay that's running (start.cmd's window and the two above do this first)
tools\account1.cmd, account2.cmd   start the game for account 1 or 2 (the dashboard's play buttons do this)
tools\unlock-client.cmd  clear the game's one-copy-at-a-time lock in running clients (launching does this)
tools\bgpatch.cmd        make the background copy of the client (Settings › Game does this)
```

Point the client at the relay: a launcher server entry with host `127.0.0.1`, port `9110`, or
`client.exe -h 127.0.0.1 -p 9110 -a user:pass`.

The companion needs the game's `portal.dat` and `cell.dat` (spell data, icons, the map); by default it looks in
`C:\Program Files (x86)\Microsoft Games\Asheron's Call` (`--dat <path to portal.dat>` for elsewhere; `cell.dat` is
read from beside it).

Relay options: `--server host:port` (default ac1999.cobradragon.com:9110), `--listen`, `--tap`,
`--upstream-ports LO-HI`, `--journal-dir`, `--journal-cap-gb` (default 10: past that the oldest finished
journals are deleted; 0 keeps everything). Companion options: `--tap`, `--web PORT`, `--dat PATH`, `--db PATH`.

Replay a journal offline: `python -m companion.replay journals/session-....cap --events`

Tests: `python -m unittest discover`

The `.cmd` files run Python through `tools\py.cmd`: the project's own `runtime\` when there is one, else an
installed Python 3.11+ that has numpy and Pillow (`pip install -r requirements.txt`), else it sets `runtime\` up
(`tools\setup.ps1`: python.org's embeddable Python, pip, `requirements.txt`). numpy and Pillow are only used to
draw the map and its walk grid from `cell.dat` the first time; they're cached in `cache\` after that.

### Launching accounts

Settings › Accounts keeps your accounts. Each has a button on the right of the header: white **▶ name** starts
`client-windows-3d-accel.exe` for it through the relay, logged in (`tools\account1.cmd` / `account2.cmd` do the same
for accounts 1 and 2); blue once it's online -- its game running on this computer (read from the running
clients' own command lines, however they were started; never the password) or connected through the relay --
and a click shows its tab. They're
kept in `accounts.local.json` (git-ignored, plain text, this computer only); the dashboard never shows a password
again, and launching takes only an account's number, so nothing can ask it to run another program. The game
folder is Settings › Game (`launcher.local.json`): found from the registry or the usual places until you pick
one. Before launching, `tools/aclaunch.py` closes the client's single-instance lock (the named semaphore
"Empyrean Client") in any running client of yours, so a second client can start, with plain Windows calls
(no administrator rights or extra tools). `tools\unlock-client.cmd` only clears the lock. The launcher refuses to
start if the relay isn't running. Check your server's rules on running more than one client.

The stock client stops drawing when its window loses focus. Settings › Game › *Make the background copy*
(or `tools\bgpatch.cmd`; Windows asks for administrator rights, since it writes into Program Files) makes
`client-windows-3d-accel-bg.exe` next to the original with one instruction removed so losing focus no longer deactivates the game; the original exe is never changed. An
account with *background* ticked (`"background": true`) starts that copy. See `tools/bgpatch.py`.

### Experimental features: vendor backgrounds

Everything above only reads the game's traffic. Settings › Game › *Enable experimental features* (off by
default) allows the one thing that changes it, on the way from the server to your game; check your server's
rules first. With it on, *Vendor backgrounds* runs the relay with `--tint-store` (start.cmd's window restarts
it; `tools\relay-tint.cmd` does the same by hand): in a vendor's window, the spell components on your
shopping list and the rest show on backgrounds you pick (Comps › Order). It rewrites only the vendor's stock
list on its way to your client (the item type that picks an icon's background); what you buy, and everything
sent to the server, is untouched.

## Updates

The companion checks the project's GitHub repository at start and every half hour. When there's a newer
commit the header says so: **Update now** brings this install up to it (Settings › Updates also has
**Check now** and *apply updates automatically*).

- A git clone updates with `git pull --ff-only`; local changes stop it rather than be overwritten.
- A downloaded copy has the new commit's files copied over it.
- Your journals, database, caches, captures, checkpoints, settings, `runtime\` and `*.local.json` are never touched,
  and nothing is deleted. The dashboard and the relay restart themselves afterwards. (A relay you started
  with `tools\relay.cmd`: the header asks you to restart it. A change to the window's own code: the header asks you
  to run `start.cmd` again when it suits you.)

The repository is `REPO` in `companion/updater.py` (or set it in Settings › Updates, kept in
`update.local.json`).

## Pages

- **Dashboard**: vitals and an XP bar (or a goal's progress), buffs with timers and what's expiring,
  components running low, bonus XP timers, the monster you're fighting (armor, resistances and the best
  damage type against it), chat.
- **Comps**: the shop list for an open vendor in its own order (what to buy, what to sell back, a budget,
  whole stacks), the component order (how much to keep, by weight or a share of what you can carry), the burn
  history, and the steps to sort components into your packs by hand.
- **Map**: the world from `cell.dat`, your position and heading, towns, settlements, lifestones, portals and
  your corpses, with routes (walking, portals and dungeons) and the direction to the next step.
- **Spell research**: every researchable spell with your account's personal formula, what's missing from
  your pack, your odds at your current skill, and a research queue ordered to need the fewest changes.
- **Buffs**: your buff profile (Relax / Max / a set tier per buff), what's missing from your spell bar, and
  which tiers to learn next.
- **Items**: everything you carry, and every item any of your characters has appraised.
- **Bonus XP**: skill-use XP grants and each skill's timer.
- **Experience**: where your XP went, and goals (unbuffed or with your buffs) with the cheapest way to reach
  them and what to spend your XP on now.
- **Info**, **Chat**, **Settings**.

## What it tracks

- **XP grants.** A grant shows up as a skill update (`0x28`) followed by unassigned/total XP steps (`0x0E`
  keys 0x16/0x15): `+S` (the skill XP, also added to unassigned), then `+D/10`, which pins the difficulty
  to a 10-point range, narrowed to an exact value from the action that caused it.
- **Component burn.** Observed burns come from the server's "The spell consumed the following components"
  text; expected burns from a model of the server's rules with the account's personalised formulas
  (decrypted from portal.dat).
- **Vendors.** Each vendor's stock and prices as its window opens; prices are worked out as the server does.

## Layout

| Module | Role |
|---|---|
| `relay.py` | Long-lived process: relay, per-client session journals, tap server |
| `__main__.py` | Restartable process: tap consumer, one tracker per session, dashboard API |
| `proxy.py` | Multi-client UDP forwarding, one upstream socket per client |
| `framing.py`, `messages.py` | Packets, fragments, reassembly; message decoders |
| `tracker.py`, `pipeline.py` | What's happening to a character, from its traffic |
| `dat.py` | portal.dat: spells (with formulas), components, skills, the XP table |
| `burn.py`, `ledger.py`, `stock.py`, `comporder.py`, `compsort.py` | Components: burn, stock, the order, sorting |
| `goals.py` | The Experience page: XP accounting and goal planning |
| `routing.py`, `walkmap.py`, `worldmap.py` | The map and routes |
| `updater.py` | Updates from GitHub |
| `storetint.py`, `netcrc.py` | The optional vendor backgrounds (relay `--tint-store`) |
| `web.py`, `static/index.html` | The dashboard (server-sent events) |
| `store.py`, `itembook.py`, `layouts.py` | SQLite history, the shared item book, per-character settings |
| `capture.py`, `replay.py` | The journal record format, and offline replay |

Write requests to the dashboard's API must carry `X-Companion: 1`, so other web pages can't use it.

Wire formats follow the ac1999 server source (gitlab.com/adamplumb/ac1999).
