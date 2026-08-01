# bbsync

Automatically downloads your lecture files from Blackboard Ultra and
organises them into a clean local folder tree. Defaults to Imperial College
London (`bb.imperial.ac.uk`) but works with **any Blackboard Ultra
instance** — see [Using a different institution](#using-a-different-institution).
Works on **macOS and Windows**.

- Mirrors each course's Blackboard folder structure under `Documents/BlackboardNotes/`
- Downloads lecture slides, notes, problem sheets and assignment files
- Collects Panopto / video links into a `videos.md` per course (videos aren't downloaded)
- Never re-downloads unchanged files; picks up updated versions automatically
- Runs in the background every 4 hours (launchd on macOS, Task Scheduler on Windows)
- **Full-text search** across every PDF, slide deck, Word doc and notebook —
  `bbsync search "divergence theorem"` tells you the course, file and page
- **Web dashboard** — `bbsync dashboard` opens a local page with everything
  above as buttons: sign in, sync, search, browse files, toggle courses,
  manage the schedule, and watch live progress. No terminal needed after setup.

## Setup (once)

Requires Python 3.11+ ([python.org](https://www.python.org/downloads/)).

**macOS** (Terminal):

```bash
git clone https://github.com/sammyschen/bbsync
cd bbsync
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium

.venv/bin/bbsync login      # opens a browser — sign in with your institution's SSO + MFA
.venv/bin/bbsync sync       # first download (may take a while)
.venv/bin/bbsync schedule install   # auto-sync every 4h from now on
```

Optional alias so you can just type `bbsync`:

```bash
echo "alias bbsync=\"$PWD/.venv/bin/bbsync\"" >> ~/.zshrc
```

**Windows** (PowerShell):

```powershell
git clone https://github.com/sammyschen/bbsync
cd bbsync
py -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\playwright install chromium

.venv\Scripts\bbsync login      # opens a browser — sign in with your institution's SSO + MFA
.venv\Scripts\bbsync sync       # first download (may take a while)
.venv\Scripts\bbsync schedule install   # auto-sync every 4h from now on
```

## Commands

| Command | What it does |
|---|---|
| `bbsync login` | Open a browser to sign in once. `--url` sets the Blackboard instance (first run only). Session is saved and reused headlessly for weeks. |
| `bbsync sync` | Download new/changed files right now. |
| `bbsync courses` | List courses. `--disable "Maths"` / `--enable "Maths"` to control which sync. |
| `bbsync index` | Build the search index (one-off; afterwards it updates itself after each sync). |
| `bbsync search TERMS` | Search all notes. `--course "Quantum"` to filter, `-n 20` for more results. |
| `bbsync schedule install` | Install the background job. Also `uninstall` / `status`. |
| `bbsync status` | Show destination, last sync time and schedule state. |
| `bbsync dashboard` | Open the web dashboard — every command above, in the browser. |

## Dashboard

```bash
bbsync dashboard          # serves http://127.0.0.1:8765 and opens your browser
```

One page, two halves: the light side is your library (full-text search with
page-level hits, a file browser, recent downloads); the navy panel is the
machine (session state, Sync now, live job log, course toggles, auto-sync
schedule and the search index). Everything the CLI does, clickable.

The server binds to `127.0.0.1` only and rejects requests from any other
host or origin — nothing is exposed to the network. `--port` picks another
port; `--no-browser` skips opening a tab (useful if you keep it running).

## Using a different institution

bbsync talks to Blackboard's own REST API, so it works anywhere Blackboard
Ultra is deployed — it isn't tied to Imperial. Point it at your institution's
Blackboard URL the first time you sign in:

```bash
bbsync login --url https://blackboard.your-university.edu
```

That's remembered in `config.toml` for every command after that (`sync`,
`dashboard`, the background schedule, …) — you only set it once.

## Configuration

`~/.bbsync/config.toml` (macOS) / `C:\Users\<you>\.bbsync\config.toml` (Windows) —
created after first login:

```toml
dest = "/Users/sammy/Documents/BlackboardNotes"     # where files go
interval_hours = 4                                   # background sync frequency
base_url = "https://bb.imperial.ac.uk"               # your institution's Blackboard Ultra

[courses."_12345_1"]
name = "Introduction to Machine Learning"
enabled = true
```

After changing `interval_hours`, run `bbsync schedule install` again to apply it.

## Searching your notes

After the one-off `bbsync index` (a few minutes), any concept is a one-second
lookup — the index then keeps itself up to date after every sync:

```
$ bbsync search perturbation theory --course Quantum
1. Lecture 14.pdf  (p.3, p.7)
   PHYS50004 - Quantum Physics 2025-2026
   p.3: … the first-order perturbation correction to the energy …
```

Search terms are stemmed ("oscillations" finds "oscillation") and implicitly
ANDed. Use double quotes for exact phrases and FTS5 operators (`OR`, `NEAR`)
for advanced queries. PDFs report page numbers, slide decks report slides,
notebooks report cells.

## When the session expires

SSO sessions typically survive for weeks (bbsync silently refreshes them on each
run). When one finally dies, the background sync sends a desktop notification —
just run `bbsync login` again.

## Platform notes

- **macOS**: schedule = launchd user agent (`~/Library/LaunchAgents/com.bbsync.sync.plist`),
  runs every N hours and at login; notifications via Notification Center.
- **Windows**: schedule = Task Scheduler task named `bbsync` (every N hours,
  runs windowless via `pythonw`); notifications are toast pop-ups. The task only
  runs while you're logged in.
- **Linux**: `login`/`sync`/`courses` work; add your own cron entry for scheduling.

## Troubleshooting

- **Logs**: background runs log to `~/.bbsync/logs/sync.log` (same path under
  your user folder on Windows).
- **"sync failed: ... ProcessSingleton"**: two syncs ran at once (the browser
  profile is locked). The next scheduled run will succeed.
- **Start fresh**: delete `~/.bbsync/manifest.json` to force re-downloading
  everything, or `~/.bbsync/browser-profile/` to force a fresh login.

## How it works

Playwright keeps a persistent Chromium profile in `~/.bbsync/browser-profile`,
so your SSO cookies survive between runs. All Blackboard calls go
through that browser context against Blackboard's own REST API
(`/learn/api/public/v1/...` — the same endpoints the Blackboard web app uses),
so bbsync only ever sees content your account can already access.
`~/.bbsync/manifest.json` records every downloaded attachment so syncs are
idempotent.

## License

MIT — see [LICENSE](LICENSE).
