# Bank VM — systemd unit install for the consolidated backend

After the backend has been consolidated to serve both browser WebSocket (`:6089`)
and SIP AudioSocket (`:6090`) in a single process, this systemd unit replaces
the old two-process `nohup` setup so the service auto-restarts on failure and
survives VM reboots.

---

## 0. Prerequisites

- The new consolidated project tree is already unzipped at
  `/home/aivoice/ubl-callcenter-gemini/`.
- The `.venv` exists at `/home/aivoice/ubl-callcenter-gemini/.venv/` and has
  all required Python packages installed (including the 2 new packages from
  the latest `requirements.txt`).
- The `.env` file with `GOOGLE_API_KEY` is in place at
  `/home/aivoice/ubl-callcenter-gemini/.env`.
- You can `sudo` on this VM.

## 1. Stop the old two-process setup first

The previous deployment ran `backend.main` and `backend.sip_server` as two
separate `nohup` processes. Stop them both before enabling the systemd unit
so ports `:6089` and `:6090` are free.

```bash
# Kill whatever is running them
pgrep -af "backend.main" ; pgrep -af "backend.sip_server"
pkill -f "backend\.sip_server" 2>/dev/null
pkill -f "backend\.main"       2>/dev/null

# Confirm both ports are free
ss -tnlp | grep -E ':6089|:6090' || echo "Both ports free."

# Clean up old PID files (optional but tidy)
rm -f /home/aivoice/ubl-callcenter-gemini/sip_server.pid \
      /home/aivoice/ubl-callcenter-gemini/backend.pid 2>/dev/null
```

## 2. Install the unit file

```bash
sudo cp /home/aivoice/ubl-callcenter-gemini/deploy/backend_service/ubl-callcenter.service \
        /etc/systemd/system/ubl-callcenter.service

sudo chown root:root /etc/systemd/system/ubl-callcenter.service
sudo chmod 644       /etc/systemd/system/ubl-callcenter.service

# Tell systemd to reload unit definitions
sudo systemctl daemon-reload
```

## 3. Enable + start

```bash
# Enable at boot + start now
sudo systemctl enable --now ubl-callcenter

# Confirm
sudo systemctl status ubl-callcenter
```

Expected status: `Active: active (running)`. If it's in `failed` state, skip
to section 6 (troubleshooting).

## 4. Verify both listeners are up

```bash
ss -tnlp | grep -E ':6089|:6090'
# Expected two lines:
#   LISTEN 0 2048 0.0.0.0:6089 0.0.0.0:* users:(("python",pid=...,fd=...))
#   LISTEN 0  100 127.0.0.1:6090 0.0.0.0:* users:(("python",pid=...,fd=...))
```

## 5. Tail the logs

```bash
# Follow live
sudo journalctl -u ubl-callcenter -f

# Last 200 lines since boot
sudo journalctl -u ubl-callcenter -n 200 --no-pager
```

## 6. Troubleshooting

### The service starts then immediately fails

```bash
sudo journalctl -u ubl-callcenter -n 80 --no-pager
```

Most common causes:
- **Python import error** — missing package in `.venv`. Fix: re-run
  `pip install --no-index --find-links=/path/to/wheels -r requirements.txt`
  inside the activated venv, then `sudo systemctl restart ubl-callcenter`.
- **`.env` missing or unreadable by `aivoice`** — check ownership of
  `/home/aivoice/ubl-callcenter-gemini/.env`.
- **Port 6089 or 6090 already bound** — an old `nohup` process wasn't killed.
  Re-run section 1, then `sudo systemctl restart ubl-callcenter`.

### SELinux denies the service

Rare on RHEL 9.7 with this setup, but if `journalctl` shows `avc: denied`:
```bash
sudo ausearch -m AVC -c python --start recent | audit2allow -M ubl-local
sudo semodule -i ubl-local.pp
sudo systemctl restart ubl-callcenter
```

## 7. Day-to-day operations

| Action | Command |
|---|---|
| Restart after a code push | `sudo systemctl restart ubl-callcenter` |
| Stop temporarily | `sudo systemctl stop ubl-callcenter` |
| Start again | `sudo systemctl start ubl-callcenter` |
| Disable auto-start at boot | `sudo systemctl disable ubl-callcenter` |
| Check uptime + PID | `sudo systemctl status ubl-callcenter` |
| Follow logs live | `sudo journalctl -u ubl-callcenter -f` |

## 8. Rollback (return to the old two-process setup)

```bash
sudo systemctl disable --now ubl-callcenter
sudo rm /etc/systemd/system/ubl-callcenter.service
sudo systemctl daemon-reload

# Then start the old way as before the consolidation:
cd /home/aivoice/ubl-callcenter-gemini
source .venv/bin/activate
nohup python -m backend.main       > backend.log     2>&1 &
nohup python -m backend.sip_server > sip_server.log  2>&1 &
```
