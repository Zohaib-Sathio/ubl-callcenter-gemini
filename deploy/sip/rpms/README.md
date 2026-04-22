# RPM payload for Asterisk install on bank VM

This directory is **empty in git** — actual `.rpm` files are dropped in here
by Zohaib (on a dev machine) before zipping Zip B. The `.rpm` binaries are
too large and platform-specific to commit to the repo, and the bank VM has
no internet to pull them at install time.

## What the bank VM is (matches for download)

- RHEL **9.7 (Plow)**, x86_64

You must download RPMs on a machine with the **same major version** (RHEL 9,
Rocky 9, or AlmaLinux 9 — all binary-compatible). A minor version mismatch
(e.g. RHEL 9.5 vs 9.7) is usually fine for base packages but may bite you
for packages linked against newer glibc.

## How to populate this folder

On a Rocky/AlmaLinux/RHEL 9 box with internet:

```bash
# 1. Enable EPEL 9 (Asterisk + AudioSocket live there)
sudo dnf install -y epel-release

# 2. Make sure your dnf cache is fresh
sudo dnf clean all && sudo dnf makecache

# 3. Download Asterisk 20 + all transitive dependencies into this folder.
#    --resolve pulls everything needed to install offline.
#    --alldeps is important: it pulls weak deps too (logging, docs, etc.)
mkdir -p ./rpm-staging
sudo dnf download --resolve --alldeps \
  --downloaddir=./rpm-staging \
  asterisk \
  asterisk-pjsip \
  asterisk-audiosocket \
  asterisk-sounds-core-en \
  asterisk-sounds-moh-opsound-wav

# 4. Copy into this deploy folder for zipping
cp ./rpm-staging/*.rpm ./
ls -lh *.rpm
```

After this, expect ~40–80 RPM files totalling 30–80 MB in this directory.

## If EPEL is locked down

If `epel-release` isn't available (some corporate mirrors), you can download
the EPEL 9 release RPM directly from a matching internet host, plus the
`fedora-epel-release` key file, and ship both. Add them to this folder and
the bank team can `dnf install` them first before installing the Asterisk
RPMs.

## Verification before zipping

Sanity-check one of the downloaded RPMs' architecture/target release:

```bash
rpm -qpi asterisk-*.x86_64.rpm | grep -E 'Architecture|Version|Release'
```

Expect `Architecture: x86_64` and a release string containing `el9`.

## Sensitive content

None of these RPMs should contain secrets. It's safe to hand the bank team
the whole directory as-is.
