#!/usr/bin/env bash
# =============================================================================
# add-sip-rules.sh — open local firewalld ports for Asterisk SIP + RTP
# =============================================================================
# Runs on the bank VM (RHEL 9). Adds rich rules restricted to the Avaya SM IP
# for SIP signaling, and a broader rule for the RTP media range (the Avaya
# media gateway IP may differ from SM; widen if needed).
#
# This is the **local** firewall on the VM. Firewall rules on the bank's
# edge / network firewall between Avaya SM and this VM are a separate
# request that goes to the bank network team.
#
# Usage:
#   sudo AVAYA_SM_IP=10.x.y.z ./add-sip-rules.sh
# =============================================================================

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must be run as root (use sudo)." >&2
    exit 1
fi

: "${AVAYA_SM_IP:?Set AVAYA_SM_IP environment variable, e.g. AVAYA_SM_IP=10.0.0.5}"

# Optional: comma-separated list of additional source IPs for RTP media
# (Avaya media gateway / G450 may send RTP from a different IP than SM).
# Defaults to the SM IP if not provided.
: "${AVAYA_MEDIA_IPS:=$AVAYA_SM_IP}"

echo ">>> Opening SIP signaling (UDP/5060) from Avaya SM ($AVAYA_SM_IP)"
firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${AVAYA_SM_IP} port port=5060 protocol=udp accept"

echo ">>> Opening RTP media range (UDP/10000-20000) from Avaya media sources"
IFS=',' read -ra MEDIA_IP_ARR <<< "$AVAYA_MEDIA_IPS"
for ip in "${MEDIA_IP_ARR[@]}"; do
    ip="${ip// /}"   # trim whitespace
    [[ -z "$ip" ]] && continue
    echo "    - from ${ip}"
    firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${ip} port port=10000-20000 protocol=udp accept"
done

echo ">>> Reloading firewalld"
firewall-cmd --reload

echo ">>> Current rich rules:"
firewall-cmd --list-rich-rules

echo ""
echo "Done. SIP signaling + RTP media are now open on this VM for the"
echo "specified Avaya source IPs. Verify with:"
echo "  ss -uln | grep -E ':5060|:1[0-9]{4}'"
