# Local environment setup (macOS / VS Code)

Merakicat reads its Meraki API key, org name, and Catalyst SSH credentials from
environment variables (see `README.md` → Usage). Instead of `export`-ing them
by hand every session, this repo now has:

- `.env` — your real values, **gitignored**, never committed
- `load-env.sh` — loads `.env` into your shell
- `.vscode/launch.json` — loads `.env` automatically when debugging in VS Code

## 1. Get a Meraki API key

1. Log into the [Meraki Dashboard](https://dashboard.meraki.com).
2. **Org admin, one-time:** Organization → Settings → *Dashboard API access* →
   check **Enable access to the Cisco Meraki Dashboard API**.
3. **Your key:** click your name (top right) → **My profile** → *API access* →
   **Generate new API key**. Copy it now — Dashboard only shows it once.

## 2. Fill in `.env`

Open `.env` in this directory and fill in the blanks:

```dotenv
MERAKI_API_KEY=your-key-here
MERAKI_ORG_NAME=your-org-name-here

# Only needed for `host`-based commands (SSH to a live switch), not for
# file-based `check`/`translate` runs against a .cfg file:
IOS_USERNAME=
IOS_PASSWORD=
IOS_SECRET=
IOS_PORT=22
```

`.env` is already covered by `.gitignore` — `git status` should never show it
as a tracked/untracked file you need to add.

## 3. Running from a Terminal (zsh, not PowerShell)

Source the loader once per terminal session, then run merakicat normally:

```zsh
cd /Users/alex.parkinson/Documents/Development/Merakicat/merakicat
source load-env.sh

cd src/merakicat
python merakicat.py translate file /path/to/CVE-PDX-Traveller.cfg to Q5VC-Y8JZ-8RBW
```

`source load-env.sh` (not `./load-env.sh`) is required — running it as a
subprocess would export the variables into a child shell that immediately
exits, so your current shell wouldn't see them.

Every new terminal tab/window needs `source load-env.sh` run again, since env
vars don't persist across shells.

## 4. Running/debugging from VS Code

`.vscode/launch.json` already has an `envFile` entry pointing at `.env`, so
hitting **Run and Debug** (▷ / F5) with the "Python: merakicat.py" config
loads `.env` automatically — no manual sourcing needed. Edit the `"args"`
list in `launch.json` if you want to pass `translate file ... to ...` style
arguments through the debugger instead of typing them in a terminal.

If you just click the "Run Python File" ▷ button in the editor (not the Run
and Debug panel), VS Code does **not** read `launch.json` — use the Run and
Debug panel, or source `load-env.sh` in the integrated terminal first and run
from there instead.
