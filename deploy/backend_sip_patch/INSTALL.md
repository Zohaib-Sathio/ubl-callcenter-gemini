# Zip A — Backend SIP Patch (Install Notes)

This patch adds **one new Python file** to the existing UBL call-center backend
so that Asterisk (shipped separately in Zip B) can stream SIP calls into the
same Gemini-powered agent the browser path already uses.

It does **NOT touch** the existing backend process. The running FastAPI app
keeps serving browser calls exactly as today. A **second, independent Python
process** starts up alongside it to handle SIP-originated calls.

---

## 1. What is in this zip

```
backend_sip_patch/
├── INSTALL.md            ← this file
└── sip_server.py         ← new file, destination: backend/sip_server.py
```

(Compared to the previously uploaded project, the only additional file is
`backend/sip_server.py`. Nothing else in `backend/` is modified.)

## 2. Prerequisites — already satisfied on the VM

- The existing UBL backend is already deployed and running on this VM.
- Python 3.11+ with all project dependencies installed (same venv the running
  backend uses).
- The `.env` file with `GOOGLE_API_KEY` is already in place.
- Outbound HTTPS to the Gemini Live endpoint is already whitelisted.

**No new Python packages are required.** This patch uses only the standard
library (`asyncio`, `audioop`, `struct`, `wave`, `uuid`) plus modules that are
already imported by the running backend.

## 3. Install — drop-in file copy

From the unzipped patch directory, on the bank VM:

```bash
# Adjust the target path if the project lives elsewhere on this VM.
PROJECT_DIR=/opt/ubl-callcenter-gemini

cp sip_server.py "$PROJECT_DIR/backend/sip_server.py"
```

That's the only file placement. Ownership/permissions should match the other
files in `backend/`:

```bash
chown <service-user>:<service-group> "$PROJECT_DIR/backend/sip_server.py"
chmod 644 "$PROJECT_DIR/backend/sip_server.py"
```

## 4. Start the new process

The existing backend keeps running as-is. Start the SIP server as a **second**
process, using the **same virtualenv** as the existing backend so imports
resolve correctly.

```bash
cd "$PROJECT_DIR"

# Activate the same virtualenv the running backend uses. Example:
source venv/bin/activate

# Start the AudioSocket server. Defaults: 127.0.0.1:6090 (loopback only).
nohup python -m backend.sip_server \
  > "$PROJECT_DIR/sip_server.log" 2>&1 &

echo $! > "$PROJECT_DIR/sip_server.pid"
```

### Optional: override host / port via env vars

```bash
export SIP_SERVER_HOST=127.0.0.1      # default; keep this for POC
export SIP_SERVER_PORT=6090           # default; change only if :6090 is taken
```

Listening on `127.0.0.1` means the port is **only reachable from this VM
itself**, so no firewall rule is needed for it. Asterisk (installed in Zip B)
will also run on this same VM and connect over loopback.

## 5. Verify it started cleanly

```bash
# Expect to see the process and the listening socket
ps -p "$(cat $PROJECT_DIR/sip_server.pid)" -o pid,cmd
ss -tnlp | grep 6090

# Expect the log to end with a line like:
#   🎧 [SIP] AudioSocket server listening on ('127.0.0.1', 6090)
tail -n 20 "$PROJECT_DIR/sip_server.log"
```

If the port is open and the log shows the "listening" line, Zip A is done.

## 6. Stop / restart

```bash
kill "$(cat $PROJECT_DIR/sip_server.pid)"
# then re-run the nohup line from step 4 to restart
```

Restarting the SIP server process does **not** affect the running FastAPI
backend (they are fully independent processes).

## 7. What this process does (for review by the bank team)

- Opens a TCP listener on `127.0.0.1:6090`.
- Accepts connections speaking Asterisk's AudioSocket protocol (one TCP
  connection per call).
- For each call:
  - Resamples caller audio 8 kHz → 16 kHz, forwards to Gemini Live.
  - Resamples Gemini output 24 kHz → 8 kHz, writes back to Asterisk.
  - Buffers keypad DTMF (for TPIN entry) and passes digits to Gemini as
    system text after 4 digits, `#`, or 2 s of silence.
  - Executes the same workflow tools the browser path uses (CNIC verify,
    TPIN verify, card activation, balance inquiry, etc.) — zero logic
    duplication; imports from the existing `backend.main`.
  - Saves user + agent WAV recordings and a `{call_id}_transcript.json`
    under `recordings/` — same format as browser calls, with
    `"source": "sip"` added.
- Uses the same `GOOGLE_API_KEY` and the same `.env` as the running backend.

## 8. Smoke-testing without Asterisk (optional, dev machine only)

A minimal Python test client can open a TCP connection to `:6090`, send an
ID frame, a few SLIN audio frames captured from a wav, then a HANGUP frame,
and check that `recordings/<call_id>_user.wav` is produced. This test is
**optional** — the real end-to-end test happens after Zip B installs
Asterisk.

## 9. Rollback

Stop the SIP server process; delete the new file:

```bash
kill "$(cat $PROJECT_DIR/sip_server.pid)"
rm "$PROJECT_DIR/backend/sip_server.pid"
rm "$PROJECT_DIR/backend/sip_server.py"
```

The existing backend is unaffected by install or rollback of this patch.
