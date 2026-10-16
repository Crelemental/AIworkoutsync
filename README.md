# Garmin-to-Obsidian Automated Fitness Pipeline

A zero-cost, fully automated pipeline that syncs Garmin Connect telemetry data into year-based JSON Lines (`.jsonl`) master files and pushes them directly to a local Windows Obsidian vault over a passwordless SSH/SCP tunnel.

---

## Architecture & Orchestration

```text
[ Garmin Connect API ]
         │
         ▼ (Cron Daily @ 4:00 AM)
[ Proxmox Debian LXC ]
   ├── garminsync.py (14-day lookback fetch, dedup & dynamic year-splitting)
   └── push.sh (Cron every 30 min; scp only when changed, retries if PC is off)
         │
         ▼ (Network Transfer over Port 22)
[ Windows PC (PUSH_HOST) ]
   └── Obsidian Vault (C:/Users/<you>/Documents/YourVaultName/)
         │
         ▼
[ Local AI / RAG Context ]
```

### Component Overview
1. **`garminsync_backlog.py`**: Run **once** manually to ingest historical Garmin data into a master year file (e.g., `2026-YTD-backlog.jsonl`).
2. **`garminsync.py`**: Runs daily to fetch recent activities, handle year-boundary edge cases, prevent duplicate entries using `activity_id`, and write cleanly sorted JSON Lines.
3. **`push.sh`**: Pushes all year files to the target Windows Obsidian vault folder whenever they've changed, retrying every 30 minutes until the PC is reachable.

---

## Prerequisites & Setup

### 1. Environment Configuration
Clone the repository into your working directory (e.g., `/root/fitness-sync/`):
```bash
git clone <your-repo-url> /root/fitness-sync
cd /root/fitness-sync
```

Create and configure your `.env` file:
```bash
cp .env.example .env
nano .env
# Fill in your Garmin credentials
```

### 2. Python Virtual Environment
Set up dependencies inside a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Windows SSH & Key Authentication (LXC to Windows)
To allow passwordless `scp` transfers from the Debian LXC to the Windows target (`PUSH_HOST` in `.env`):
* Generate an SSH key on the LXC (`ssh-keygen`).
* Copy the public key to the Windows Administrator OpenSSH authorized keys path:
  `C:\ProgramData\ssh\administrators_authorized_keys`
* Ensure strict permissions are applied using `icacls` on Windows so the OpenSSH service accepts the key.

---

## Automation (Cron Scheduling)

Fetching and pushing are two separate jobs, so the Windows PC doesn't need to be on at 4:00 AM:

* **Fetch (daily, 4:00 AM):** `garminsync.py` looks back `LOOKBACK_DAYS` days (default 14, can be set in `.env`). The `activity_id` dedup means missed runs and late watch syncs are caught on the next run without creating duplicates.
* **Push (every 30 min):** `push.sh` copies every `output/*.jsonl` file to the vault, but only if something changed since the last successful push. If the PC is off, it logs one line and tries again on the next run.

Set `PUSH_USER`, `PUSH_HOST` and `PUSH_DEST` in `.env` (see `.env.example`), make the script executable with `chmod +x push.sh`, then open your crontab on the LXC:
```bash
crontab -e
```

Add:
```bash
0 4 * * * /root/fitness-sync/.venv/bin/python /root/fitness-sync/garminsync.py >> /root/fitness-sync/output/cron.log 2>&1
*/30 * * * * /root/fitness-sync/push.sh >> /root/fitness-sync/output/cron.log 2>&1
```

> **Note:** cron treats an unescaped `%` as a newline. If you add commands like `date +%Y` to a crontab line, write them as `date +\%Y`.

### Checking Logs
To verify execution history and troubleshoot transfers:
```bash
cat /root/fitness-sync/output/cron.log
```