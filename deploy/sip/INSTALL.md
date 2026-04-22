# Zip B — Asterisk SIP Bridge (Install Notes)

This zip installs **Asterisk 18 (LTS)** on the bank VM and configures it to
receive SIP INVITEs from Avaya Session Manager, then bridge the call's audio
to the Python SIP server installed by **Zip A** (which must already be running
on this VM at `127.0.0.1:6090`).

> Note: EPEL 9 ships Asterisk 18.x, not 20.x. AudioSocket modules are
> included in the main `asterisk` RPM. All configs in this zip are written
> against Asterisk 18 and will work as-is. If the bank ever wants Asterisk 20
> or newer, that will need an out-of-EPEL install path (SignalWire RPMs or
> source build).

## 0. Prerequisites (confirm before starting)

| Item | Expected value |
|---|---|
| VM OS | RHEL 9.7 (Plow), x86_64 |
| Zip A already installed | Yes — `python -m backend.sip_server` running on `:6090` |
| Avaya SM IP | **TBD** — ask Avaya team. This is the SIP peer's IP. |
| DID (phone number) | **TBD** — the number customers will dial. |
| Local firewall | firewalld running (`systemctl status firewalld`) |
| Bank edge firewall request | Filed — UDP/5060 + UDP/10000–20000 from Avaya SM → this VM |

If any of the "TBD" items aren't known yet, install steps 1–3 can still be
done; steps 4+ need the Avaya details.

## 1. What is in this zip

```
sip/
├── INSTALL.md             ← this file
├── asterisk/
│   ├── pjsip.conf         → /etc/asterisk/pjsip.conf
│   ├── extensions.conf    → /etc/asterisk/extensions.conf
│   ├── rtp.conf           → /etc/asterisk/rtp.conf
│   └── modules.conf       → /etc/asterisk/modules.conf
├── firewalld/
│   └── add-sip-rules.sh   (run once to open local firewall)
└── rpms/
    └── *.rpm              (Asterisk 20 + all dependencies, offline install)
```

## 2. Install Asterisk from the offline RPMs

```bash
cd /path/to/unzipped/sip/rpms

# Installs Asterisk and every bundled dependency in one shot, no network.
sudo dnf install -y ./*.rpm

# Verify
asterisk -V
# Expected:  Asterisk 18.x.y
```

If `dnf` complains about missing keys, add `--nogpgcheck` (the RPMs are
known-safe and came from your own dev host):

```bash
sudo dnf install -y --nogpgcheck ./*.rpm
```

## 3. Verify the AudioSocket module is present

```bash
rpm -ql asterisk-audiosocket | grep '\.so$'
# Expected output (paths may differ slightly):
#   /usr/lib64/asterisk/modules/app_audiosocket.so
#   /usr/lib64/asterisk/modules/res_audiosocket.so
```

If missing, go back and check that `asterisk-audiosocket-*.rpm` was in the
zip's `rpms/` folder.

## 4. Place the configuration files

```bash
cd /path/to/unzipped/sip/asterisk

# Back up the factory configs once (in case rollback is ever needed)
sudo cp /etc/asterisk/pjsip.conf        /etc/asterisk/pjsip.conf.factory
sudo cp /etc/asterisk/extensions.conf   /etc/asterisk/extensions.conf.factory
sudo cp /etc/asterisk/rtp.conf          /etc/asterisk/rtp.conf.factory
sudo cp /etc/asterisk/modules.conf      /etc/asterisk/modules.conf.factory

# Install our configs
sudo cp pjsip.conf       /etc/asterisk/pjsip.conf
sudo cp extensions.conf  /etc/asterisk/extensions.conf
sudo cp rtp.conf         /etc/asterisk/rtp.conf
sudo cp modules.conf     /etc/asterisk/modules.conf

sudo chown asterisk:asterisk /etc/asterisk/*.conf
sudo chmod 640 /etc/asterisk/*.conf
```

### 4a. Fill in the Avaya SM IP

Edit `/etc/asterisk/pjsip.conf` and replace `__AVAYA_SM_IP__` with the real
Avaya SM IP in **two places**:

```bash
sudo sed -i 's/__AVAYA_SM_IP__/10.0.0.5/g' /etc/asterisk/pjsip.conf
#                                ^^^^^^^^  replace with real IP
```

If this VM is behind NAT from Avaya's perspective (it shouldn't be inside a
bank network), also set `external_media_address` and `external_signaling_address`
in the `[transport-udp]` block of `pjsip.conf`.

## 5. Open the local firewall

```bash
cd /path/to/unzipped/sip/firewalld
sudo AVAYA_SM_IP=10.0.0.5 bash ./add-sip-rules.sh
#                  ^^^^^^^^ replace with real IP

# If Avaya's media gateway (G450) sends RTP from a different IP than SM,
# pass both (comma-separated):
sudo AVAYA_SM_IP=10.0.0.5 AVAYA_MEDIA_IPS=10.0.0.5,10.0.0.6 bash ./add-sip-rules.sh
```

Verify:

```bash
sudo firewall-cmd --list-rich-rules
sudo ss -uln | grep -E ':5060'
```

## 6. SELinux — usually no change needed

Asterisk's own RPM ships an SELinux policy module. To confirm nothing is
being blocked silently:

```bash
sudo ausearch -m AVC -c asterisk --start recent 2>/dev/null | tail
```

If you see `avc: denied` entries after the first test call, generate a
local policy:

```bash
sudo ausearch -m AVC -c asterisk --start recent | audit2allow -M asterisk_local
sudo semodule -i asterisk_local.pp
```

## 7. Start Asterisk

```bash
sudo systemctl enable --now asterisk
sudo systemctl status asterisk
# Expected:   Active: active (running)
```

Tail the log:

```bash
sudo tail -f /var/log/asterisk/full
```

Leave this open in a second terminal during the first test call.

## 8. Sanity checks before a real call

### 8a. Asterisk CLI is reachable

```bash
sudo asterisk -rvv
# Prompt: "*CLI>"
```

### 8b. AudioSocket module loaded

```
*CLI> module show like audiosocket
Module                         Description
app_audiosocket.so             AudioSocket Application
res_audiosocket.so             AudioSocket support for Asterisk

2 modules loaded
```

### 8c. PJSIP endpoint is known

```
*CLI> pjsip show endpoints
Endpoint:  avaya-sm         Unavailable   0 of inf
```

Initially shows "Unavailable" — it flips to "Not in use" once Asterisk has
exchanged SIP OPTIONS with Avaya SM successfully.

### 8d. Python SIP server is listening (from Zip A)

```bash
ss -tnlp | grep 6090
# Expected:  LISTEN 0  128  127.0.0.1:6090   0.0.0.0:*
```

## 9. First real call test

1. Ask the Avaya team to route the test DID to this VM's IP:5060.
2. Call the DID from a real phone.
3. In one terminal: `sudo asterisk -rvvvvv` — watch verbose output.
4. In another: `tail -f /path/to/project/sip_server.log` — watch the Python
   side.

Expected happy path:

```
*CLI> NOTICE[...] res_pjsip_session.c: Incoming call from ... to ...
*CLI> [...] NoOp: Inbound call: to=... from=... uid=...
*CLI> [...] Answer on SIP/avaya-sm-00000001
*CLI> [...] AudioSocket: uuid=... host=127.0.0.1:6090
```

and in `sip_server.log`:

```
📞 [SIP] Connection from ('127.0.0.1', ...)
📞 [SIP] Asterisk channel UUID: ...
✅ Connected to Gemini Live API (model: gemini-2.5-...)
🔊 [SIP] Agent said: <greeting text>
```

If the caller hears the greeting — **you're done with Zip B's happy path**.

## 10. Common failure modes + fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `pjsip show endpoints` stays "Unavailable" | SIP OPTIONS not reaching Avaya, or Avaya's reply not reaching us | Check bank edge firewall. Run `tcpdump -i any -n udp port 5060` on the VM. |
| Call connects but silent | Media flowing but not AudioSocket path, or codec mismatch | Check `*CLI> core show channels verbose`. Codec should be `ulaw<->slin` translated. |
| `AudioSocket: Failed to connect` in logs | Python server not listening on 6090 | `ss -tnlp | grep 6090` → restart Zip A if missing. |
| `app_audiosocket.so: unable to load` | RPM not installed | `rpm -qa | grep audiosocket` → reinstall `asterisk-audiosocket-*.rpm`. |
| Caller hears greeting but not responses | One-way audio — check RTP range open in firewall | `firewall-cmd --list-rich-rules` → confirm 10000-20000/udp. |
| Caller hangs up immediately | Asterisk rejecting call (400/488 SIP) | Check `/var/log/asterisk/full` right after call attempt. |

## 11. Rollback

```bash
sudo systemctl stop asterisk
sudo systemctl disable asterisk
sudo cp /etc/asterisk/*.factory /etc/asterisk/    # rename back as needed
sudo dnf remove -y asterisk\*                     # if uninstalling entirely
```

Zip A keeps running the whole time and is unaffected.
