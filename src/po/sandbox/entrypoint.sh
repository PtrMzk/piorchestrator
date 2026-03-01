#!/bin/bash
set -euo pipefail

# --- Network isolation ---
# Collect all allowed IPs from /etc/hosts (injected via --add-host).
# These are the ONLY remote hosts the agent can reach on port 443.
ALLOWED_IPS=$(grep -E 'api\.anthropic\.com|pypi\.org|pythonhosted\.org|npmjs\.org' /etc/hosts \
    | awk '{print $1}' | sort -u)

if [ -z "$ALLOWED_IPS" ]; then
    echo "FATAL: No allowed IPs found in /etc/hosts." >&2
    echo "The container was not started with --add-host flags." >&2
    echo "Refusing to run without network isolation." >&2
    exit 1
fi

# Validate each IP before trusting it
for ip in $ALLOWED_IPS; do
    if ! echo "$ip" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
        echo "FATAL: Invalid IP in /etc/hosts: $ip" >&2
        exit 1
    fi
done

# --- IPv4 firewall ---
# Allow loopback
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Allow established/related connections
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow HTTPS (443) to each allowlisted IP
for ip in $ALLOWED_IPS; do
    iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
done

# Allow DNS so package managers can resolve CDN hostnames
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

# Set default policies to DROP — this is the critical line.
# Even if rules are flushed, the default is deny.
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# --- IPv6 firewall ---
# IPv6 is disabled via sysctl, but belt-and-suspenders: drop everything.
ip6tables -P INPUT DROP 2>/dev/null || true
ip6tables -P FORWARD DROP 2>/dev/null || true
ip6tables -P OUTPUT DROP 2>/dev/null || true

# --- Firewall verification ---
# Test 1: Verify a non-allowlisted host is blocked
if curl --connect-timeout 3 -s https://example.com >/dev/null 2>&1; then
    echo "FATAL: Firewall verification failed — example.com is reachable." >&2
    echo "Network isolation is not working. Refusing to run." >&2
    exit 1
fi

# Test 2: Verify an allowlisted host is reachable
if ! curl --connect-timeout 5 -s "https://api.anthropic.com/" >/dev/null 2>&1; then
    echo "WARNING: Cannot reach api.anthropic.com — agent may fail to authenticate." >&2
    # Don't exit — the IPs may have changed but DNS is allowed, so
    # the agent might still work via DNS resolution at runtime.
fi

echo "Firewall verified: $(echo "$ALLOWED_IPS" | wc -w | tr -d ' ') IPs allowed, non-allowlisted hosts blocked."

# --- Git configuration ---
git config --global --add safe.directory '*'
git config --global user.name "po-agent"
git config --global user.email "po-agent@localhost"

# --- Claude config ---
# Auth credentials are in the mounted volume at /home/agent/.claude
# Ensure onboarding is bypassed (critical for headless use)
if [ ! -f /home/agent/.claude.json ]; then
    echo '{"hasCompletedOnboarding":true}' > /home/agent/.claude.json
fi
chown -R agent:agent /home/agent/.claude /home/agent/.claude.json 2>/dev/null || true

# --- Environment ---
export NODE_OPTIONS="--max-old-space-size=4096"
export USE_BUILTIN_RIPGREP=0

exec su-exec agent "$@"
