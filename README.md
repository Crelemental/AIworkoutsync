# Garmin-to-Obsidian Automated Fitness Pipeline

A zero-cost, fully automated pipeline that syncs Garmin Connect telemetry data into year-based JSON Lines (`.jsonl`) master files and pushes them directly to a local Windows Obsidian vault over a passwordless SSH/SCP tunnel.

---

## Architecture & Orchestration

[ Garmin Connect API ]
│
▼ (Cron Daily @ 4:00 AM)
[ Proxmox Debian LXC ]
├── garminsync.py (Incremental daily fetch & dynamic year-splitting)
└── scp (Cross-platform SSH transport via forward slashes)
│
▼ (Network Transfer over Port 22)
[ Windows PC (main computer) ]
└── Obsidian Vault (C:/Users/[your_user]/Documents/YourVaultName/)
│
▼
[ Local AI / RAG Context ]


### Component Overview
1. **`garminsync_backlog.py`**: Run **once** manually to ingest historical Garmin data into a master year file (e.g., `2026-YTD-backlog.jsonl`).
2. **`garminsync.py`**: Runs daily to fetch recent activities, handle year-boundary edge cases, prevent duplicate entries using `activity_id`, and write cleanly sorted JSON Lines.
3. **Automated Cron & SCP**: Evaluates the current year dynamically (`$(date +%Y)`) and pushes the updated file straight to the target Windows Obsidian vault folder.

---

## Prerequisites & Setup

### 1. Environment Configuration
Clone the repository into your working directory (e.g., `/root/fitness-sync/`):
```bash
git clone <your-repo-url> /root/fitness-sync
cd /root/fitness-sync
Create and configure your .env file:

Bash
cp .env.example .env
nano .env
# Fill in your Garmin credentials
2. Python Virtual Environment
Set up dependencies inside a virtual environment:

Bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
3. Windows SSH & Key Authentication (LXC to Windows)
To allow passwordless scp transfers from the Debian LXC to the Windows target ([host_IP]):

Generate an SSH key on the LXC (ssh-keygen).

Copy the public key to the Windows Administrator OpenSSH authorized keys path:
C:\ProgramData\ssh\administrators_authorized_keys

Ensure strict permissions are applied using icacls on Windows so the OpenSSH service accepts the key.

Automation (Cron Scheduling)
To run the pipeline automatically every day at 4:00 AM, open your crontab on the LXC:

Bash
crontab -e
Add the following cron job (replace YourVaultName with your actual Obsidian vault directory name):

Bash
0 4 * * * /root/fitness-sync/.venv/bin/python /root/fitness-sync/garminsync.py && YR=$(date +\%Y) && scp -v /root/fitness-sync/output/${YR}-YTD-backlog.jsonl user@host_IP:"C:/Users/user/Documents/YourVaultName/" >> /root/fitness-sync/output/cron.log 2>&1

# Checking Logs
To verify execution history and troubleshoot transfers:

Bash
cat /root/fitness-sync/output/cron.log