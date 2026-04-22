#!/bin/bash
# =============================================================================
# download-rpms.sh — run inside a Rocky/RHEL 9 container to populate ../rpms/
# with Asterisk + dependencies for offline install on the bank VM.
#
# Invoke via:
#   docker run --rm \
#     -v "$(pwd)/deploy/sip/rpms:/rpms" \
#     -v "$(pwd)/deploy/sip/download-rpms.sh:/download-rpms.sh:ro" \
#     rockylinux:9 bash -c "tr -d '\r' </download-rpms.sh | bash"
#
# Deliberately does NOT use `set -e` — we want to see every failure, not
# abort on the first missing package.
# =============================================================================

echo "=== Installing EPEL + dnf-plugins-core ==="
dnf install -y epel-release dnf-plugins-core >/tmp/epel-install.log 2>&1 && echo "  installed." || {
    echo "  FAILED. Tail of log:"
    tail -20 /tmp/epel-install.log
    exit 1
}
dnf makecache >/dev/null 2>&1
echo ""

echo "=== Discovering available Asterisk packages in EPEL 9 ==="
all_pkgs=$(dnf repoquery --qf '%{name}\n' 'asterisk*' 2>/dev/null | sort -u)
if [ -z "$all_pkgs" ]; then
    echo "ERROR: no asterisk packages visible. EPEL may not be enabled."
    exit 1
fi
echo "$all_pkgs" | sed 's/^/  /'
echo ""

echo "=== Filtering: drop devel/debuginfo/debugsource/doc variants ==="
pkgs=$(echo "$all_pkgs" | grep -vE 'debuginfo|debugsource|devel|-doc$')
echo "Will download:"
echo "$pkgs" | sed 's/^/  /'
echo ""

echo "=== Downloading each package + its transitive dependencies ==="
for pkg in $pkgs; do
    if dnf download --resolve --alldeps --downloaddir=/rpms "$pkg" >/tmp/dl.log 2>&1; then
        echo "  OK  $pkg"
    else
        echo "  --  $pkg (skipped: $(tail -1 /tmp/dl.log))"
    fi
done
echo ""

echo "=== Checking downloaded RPMs for AudioSocket modules ==="
found_audiosocket=0
for rpm in /rpms/asterisk-*.rpm /rpms/asterisk*.rpm; do
    [ -f "$rpm" ] || continue
    if rpm -qlp "$rpm" 2>/dev/null | grep -q audiosocket; then
        echo "  FOUND in $(basename "$rpm")"
        found_audiosocket=1
    fi
done
echo ""
if [ "$found_audiosocket" = "1" ]; then
    echo "PASS: AudioSocket modules are bundled in the downloaded RPMs."
    echo "      Zip B is good to ship once this finishes."
else
    echo "FAIL: AudioSocket .so files are NOT in any downloaded RPM."
    echo "      EPEL 9's Asterisk package does not include AudioSocket support."
    echo "      Next step: compile res_audiosocket.so + app_audiosocket.so from"
    echo "      the matching Asterisk source tarball and ship the .so files"
    echo "      alongside the RPMs. Report this outcome back."
fi
echo ""

echo "=== Final RPM inventory ==="
rpm_count=$(ls /rpms/*.rpm 2>/dev/null | wc -l)
echo "  total files: $rpm_count"
du -sh /rpms 2>/dev/null | sed 's/^/  total size: /'
echo ""
echo "  first 10 RPMs:"
ls -la /rpms/*.rpm 2>/dev/null | head -10 | sed 's/^/    /'
