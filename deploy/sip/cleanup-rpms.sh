#!/bin/bash
# =============================================================================
# cleanup-rpms.sh — run ON THE BANK VM inside /home/aivoice/sip/rpms
#
# Purpose: The RPM bundle was downloaded inside a Rocky 9 container but the
# bank VM runs RHEL 9.7. Many of the bundled RPMs are core OS packages whose
# Rocky build numbers conflict with RHEL. This script moves those aside, plus
# the Asterisk subpackages we don't need, so only safe-to-install RPMs remain.
#
# Usage (from anywhere on the VM, after this script has been uploaded):
#   cd /home/aivoice/sip/rpms   # or wherever the RPMs live
#   bash /path/to/cleanup-rpms.sh
#
# After it finishes, run:
#   sudo dnf install -y ./*.rpm
# =============================================================================

set -u

# Resolve script location — allows `bash cleanup-rpms.sh` or `./cleanup-rpms.sh`
# The script expects to run in the directory containing the RPMs.
TARGET_DIR="${1:-$(pwd)}"
cd "$TARGET_DIR" || { echo "ERROR: cannot cd into $TARGET_DIR"; exit 1; }

echo "=== Quarantining core OS packages (already present on RHEL 9.7) ==="
mkdir -p _do_not_install

core_os_patterns=(
    'rocky-release*'
    'redhat-release*'
    'systemd-*'
    'systemd.*'
    'glibc-*'
    'glibc.*'
    'openssl-*'
    'openssl.*'
    'nss-*'
    'nss.*'
    'nspr-*'
    'openldap-*'
    'openldap.*'
    'cyrus-sasl-*'
    'cyrus-sasl.*'
    'pipewire-*'
    'jack-audio-connection-kit-*'
    'libreswan-*'
    'device-mapper-*'
    'lvm2-*'
    'util-linux-*'
    'cryptsetup-*'
    'kernel-*'
    'audit-*'
    'crypto-policies-*'
    'coreutils-*'
    'pam-*'
    'bash-*'
    'filesystem-*'
    'setup-*'
    'rpm-*'
    'python3-*'
    'python3.*'
    'dbus-*'
    'libxcrypt-*'
    'libgcrypt-*'
    'gnutls-*'
    'libselinux-*'
    'libsepol-*'
    'ncurses-*'
    'readline-*'
)

for pat in "${core_os_patterns[@]}"; do
    mv $pat _do_not_install/ 2>/dev/null || true
done

echo "  moved $(ls _do_not_install 2>/dev/null | wc -l) core OS RPMs aside."
echo ""

echo "=== Quarantining Asterisk subpackages we don't need ==="
mkdir -p _unused_asterisk

unused_asterisk_patterns=(
    'asterisk-mwi-external-*'
    'asterisk-voicemail-*'
    'asterisk-ldap-*'
    'asterisk-mysql-*'
    'asterisk-odbc-*'
    'asterisk-tds-*'
    'asterisk-corosync-*'
    'asterisk-calendar-*'
    'asterisk-unistim-*'
    'asterisk-alsa-*'
)

for pat in "${unused_asterisk_patterns[@]}"; do
    mv $pat _unused_asterisk/ 2>/dev/null || true
done

echo "  moved $(ls _unused_asterisk 2>/dev/null | wc -l) unused Asterisk RPMs aside."
echo ""

echo "=== Remaining RPMs ready to install ==="
remaining_count=$(ls *.rpm 2>/dev/null | wc -l)
echo "  total: $remaining_count"
echo "  asterisk-family RPMs:"
ls asterisk*.rpm 2>/dev/null | sed 's/^/    /'
echo ""

echo "=== NEXT STEPS ==="
echo "  1. Install what's left:"
echo "       sudo dnf install -y ./*.rpm"
echo ""
echo "  2. If dnf still complains about ONE specific package, move that RPM"
echo "     into _do_not_install/ manually and retry:"
echo "       mv <offending-package>-*.rpm _do_not_install/"
echo "       sudo dnf install -y ./*.rpm"
echo ""
echo "  3. Verify Asterisk is installed:"
echo "       asterisk -V"
echo "       rpm -ql asterisk | grep audiosocket"
