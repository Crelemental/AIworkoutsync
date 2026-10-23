# Garmin-to-Obsidian Automated Fitness Pipeline

A zero-cost, fully automated pipeline that syncs Garmin Connect telemetry data into year-based JSON Lines (`.jsonl`) master files and pushes them directly to a local Windows Obsidian vault over a passwordless SSH/SCP tunnel.

---

## Architecture & Orchestration

```text
[ Garmin Connect API ]
         │
         ▼ (Cron Daily @ 4:00 AM)
[ Proxmox Debian LXC ]
   ├── garminsync.py (7-day lookback fetch, dedup & dynamic year-splitting)
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
1. **`garminsync_backlog.py`**: Run **once** manually to ingest the current year's history: every activity, plus daily wellness for every day since Jan 1.
2. **`garminsync.py`**: Runs daily. Fetches new activities from the lookback window (deduplicated by `activity_id`), then refreshes daily wellness for the same window up to yesterday.
3. **`records.py`**: Builds one activity record. Besides the basics and per-lap pace, HR, cadence and stride, each record includes HR time-in-zones (with Garmin's bpm boundaries), a run/walk interval breakdown (each run with its peak HR, each recovery walk with the HR it dropped to, separate from any warm-up or cool-down walk, plus Performance Condition), per-lap power and running dynamics, avg/moving pace and total ascent/descent, self-evaluation (feel and perceived effort), primary benefit, intensity minutes, Body Battery impact and stamina.
4. **`wellness.py`**: Builds one record per calendar day, rest days included: sleep (times, score, stages vs Garmin's optimal ranges, SpO2, respiration), HRV vs baseline, resting HR, Body Battery, stress, steps and movement, training readiness and its factors, VO2 max, training status, acute vs chronic load and monthly load balance. Each day lists its workouts by `activity_id`.
5. **`store.py`**: Reads and writes the JSONL files (compact JSON, atomic writes).
6. **`push.sh`**: Pushes all year files to the target Windows Obsidian vault folder whenever they've changed, retrying every 30 minutes until the PC is reachable.

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

* **Fetch (daily, 4:00 AM):** `garminsync.py` looks back `LOOKBACK_DAYS` days (default 7, can be set in `.env`). The `activity_id` dedup means missed runs and late watch syncs are caught on the next run without creating duplicates.
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

### Output Files
Both are written to `output/`, one file per year, one JSON object per line, with `null` for values Garmin doesn't have:
* **`{year}-YTD-backlog.jsonl`**: one line per activity.
* **`{year}-daily-wellness.jsonl`**: one line per day. Sleep is the night that ended on that date. Training status and load are end-of-day values, so they include that day's workouts.

> **Training load:** Garmin's chronic load can stop updating while acute load keeps changing (it stayed at 137 from Aug 18 to Oct 21, 2026), which also freezes Garmin's ratio and "Optimal" label. So `training_load.chronic_estimated` is computed from Garmin's acute load as a 28-day exponentially weighted average, along with the ratio, status (Low < 0.8 ≤ Optimal ≤ 1.5 < High) and optimal acute range. Garmin's own values are kept under `garmin_reported`, with `chronic_unchanged_since` showing when they went stale. Estimates start once there are 28 days of history.

### Checking Logs
To verify execution history and troubleshoot transfers:
```bash
cat /root/fitness-sync/output/cron.log
```