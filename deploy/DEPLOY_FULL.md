# Full-project redeployment on the bank VM

This document covers deploying the **consolidated backend** (single process
serving both browser WebSocket on `:6089` and SIP AudioSocket on `:6090`)
to the closed bank VM, on top of the already-installed Asterisk (from Zip B).

The zip contains the complete project tree plus a `wheels/` folder with all
Python dependencies pre-downloaded for Python 3.11 on RHEL 9.7. No internet
needed on the VM at install time.

---

## 0. What the zip contains

```
ubl-callcenter-gemini/
├── backend/                      code (main.py, sip_server.py, services/, workflow/, utils/, logger/)
├── frontend/                     browser voice client
├── pages/
├── deploy/
│   ├── DEPLOY_FULL.md           ← this file
│   ├── backend_service/         systemd unit + install notes
│   └── sip/                     Asterisk configs + firewall script (already applied on VM; kept for reference)
├── requirements.txt
├── Dockerfile, docker-compose.yml
└── wheels/                       ~40-80 .whl files, ~200-400 MB
```

## 1. Preflight — prerequisites

- You already installed Asterisk on this VM (from Zip B) and it is running on
  UDP/5060 with AudioSocket configured to forward to `127.0.0.1:6090`.
- The VM has `python3.11` available: `python3.11 --version` should print `3.11.x`.
  If missing: `sudo dnf install -y python3.11 python3.11-pip` (needs RHEL
  AppStream repo to have been enabled at install time).
- The current running project lives at `/home/aivoice/ubl-callcenter-gemini/`.

## 2. Stop the currently running services

```bash
# Stop the two current Python processes
pkill -f "backend\.sip_server" 2>/dev/null
pkill -f "backend\.main"       2>/dev/null

# Confirm both ports are free
ss -tnlp | grep -E ':6089|:6090' || echo "OK: both ports free."

# Asterisk stays running — leave it alone
sudo systemctl status asterisk     # should still be active
```

## 3. Back up the old project (for rollback if needed)

Keep the old tree and its venv intact in case you need to roll back fast:

```bash
cd /home/aivoice
mv ubl-callcenter-gemini "ubl-callcenter-gemini.backup.$(date +%F-%H%M)"
ls -la | head -5                    # confirm backup renamed
```

## 4. Upload + unpack the new zip

Upload `ubl-callcenter-gemini-full.zip` to `/home/aivoice/` via Horizon
(same mechanism as prior zips). Then:

```bash
cd /home/aivoice

# Permissions (the zip was created by another user via Horizon)
sudo chown aivoice:aivoice ubl-callcenter-gemini-full.zip
sudo chmod 644 ubl-callcenter-gemini-full.zip

# Unpack — this creates the ubl-callcenter-gemini/ tree
unzip ubl-callcenter-gemini-full.zip

# Sanity-check
cd ubl-callcenter-gemini
ls -la
ls wheels/ | wc -l                  # expect 40-80 .whl files
cat requirements.txt                # expect 27 packages, chromadb present, pinecone absent
```

## 5. Create a fresh Python 3.11 venv

A clean slate avoids any package drift from the previous venv.

```bash
cd /home/aivoice/ubl-callcenter-gemini

# Create new venv (dot-prefix to match prior convention)
python3.11 -m venv .venv

# Activate
source .venv/bin/activate
python --version                    # must say Python 3.11.x

# Upgrade pip itself (using the pip wheel in our bundle if present)
pip install --no-index --find-links=./wheels --upgrade pip setuptools wheel 2>/dev/null || \
  pip install --upgrade --no-index --find-links=./wheels pip || true
```

## 6. Install all dependencies from the offline wheels

```bash
# Still inside the activated .venv
pip install --no-index --find-links=./wheels -r requirements.txt

# Verify the headline packages
pip show chromadb | head -2
pip show resemblyzer | head -2
pip show google-genai | head -2
pip show fastapi | head -2

# pinecone should NOT be installed
pip show pinecone 2>/dev/null && echo "WARN: pinecone is installed but not in requirements.txt" || echo "OK: no pinecone"
```

If any `pip install` line errors with "No matching distribution found": that
wheel is missing from the bundle. Tell me which one and I'll re-download.

## 7. Copy your existing `.env` into the new tree

The old `.env` has your `GOOGLE_API_KEY` and `JWT_SECRET_KEY` — it lives in
the `.backup` dir from step 3.

```bash
cp "/home/aivoice/ubl-callcenter-gemini.backup.$(ls -1d /home/aivoice/ubl-callcenter-gemini.backup.* | tail -1 | xargs basename | sed 's/ubl-callcenter-gemini.backup.//')/.env" \
   /home/aivoice/ubl-callcenter-gemini/.env

# Or just:
# cp /home/aivoice/ubl-callcenter-gemini.backup.*/.env /home/aivoice/ubl-callcenter-gemini/.env

# Confirm it contains the API keys
grep -E "^(GOOGLE_API_KEY|JWT_SECRET_KEY)" /home/aivoice/ubl-callcenter-gemini/.env
```

## 8. Install and start the systemd service

```bash
cd /home/aivoice/ubl-callcenter-gemini/deploy/backend_service

# Install unit
sudo cp ubl-callcenter.service /etc/systemd/system/ubl-callcenter.service
sudo chown root:root /etc/systemd/system/ubl-callcenter.service
sudo chmod 644       /etc/systemd/system/ubl-callcenter.service

# Reload systemd + enable + start
sudo systemctl daemon-reload
sudo systemctl enable --now ubl-callcenter

# Verify
sudo systemctl status ubl-callcenter
```

Expected `Active: active (running)`. If `failed`, jump to section 10.

## 9. Verify both listeners are up

```bash
ss -tnlp | grep -E ':6089|:6090'
# Expected two lines:
#   LISTEN 0 2048 0.0.0.0:6089 ...   (uvicorn HTTP/WS)
#   LISTEN 0  100 127.0.0.1:6090 ... (AudioSocket for Asterisk)

# Live logs
sudo journalctl -u ubl-callcenter -f --no-pager
```

Look for the `🎧 [SIP] AudioSocket server listening on ('127.0.0.1', 6090)`
line in the logs — that confirms the consolidation worked.

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `failed` state on `systemctl status` | Python import error (missing wheel) | `sudo journalctl -u ubl-callcenter -n 80 --no-pager`. The traceback names the missing module. Re-install via `pip install --no-index --find-links=./wheels <pkg>`. Restart: `sudo systemctl restart ubl-callcenter`. |
| Only `:6089` listening, `:6090` missing | Circular import or sip_server import error | `journalctl` will show the traceback. Look for "ImportError" near startup. |
| Only `:6090` listening, `:6089` missing | uvicorn crashed; SIP bridge orphaned | Restart service. If it recurs, check `.env` for GOOGLE_API_KEY. |
| Service runs but browser calls fail | Expected — test with browser first by loading the voice-client HTML | N/A |
| Asterisk call lands but silent | Asterisk is trying to reach :6090 on a port that's not listening | Check `ss -tnlp \| grep 6090` and restart service if missing. |
| `No matching distribution` during step 6 | Wheel missing from bundle | Report the package name back — we'll ship a patch wheel. |

## 11. Rollback (if anything goes badly)

```bash
sudo systemctl disable --now ubl-callcenter
sudo rm /etc/systemd/system/ubl-callcenter.service
sudo systemctl daemon-reload

cd /home/aivoice
mv ubl-callcenter-gemini               ubl-callcenter-gemini.bad.$(date +%s)
mv ubl-callcenter-gemini.backup.*      ubl-callcenter-gemini

# Start the old two-process way
cd ubl-callcenter-gemini
source .venv/bin/activate
nohup python -m backend.main       > backend.log    2>&1 &
nohup python -m backend.sip_server > sip_server.log 2>&1 &

ss -tnlp | grep -E ':6089|:6090'
```

## 12. What to do after successful deploy

- Delete `ubl-callcenter-gemini.backup.*` after a week of stable operation.
- `rm /home/aivoice/ubl-callcenter-gemini-full.zip` — zip no longer needed once the tree is on disk.
- Place a real test call end-to-end (Avaya → SIP → Asterisk → service → Gemini → back).
