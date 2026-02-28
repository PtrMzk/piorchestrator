#!/bin/bash
set -e

# --- Network isolation ---
# Collect all allowed IPs from /etc/hosts (injected via --add-host)
ALLOWED_IPS=$(grep -E 'api\.anthropic\.com|pypi\.org|pythonhosted\.org|npmjs\.org' /etc/hosts \
    | awk '{print $1}' | sort -u)

if [ -n "$ALLOWED_IPS" ]; then
    iptables -A INPUT -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

    for ip in $ALLOWED_IPS; do
        iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
    done

    # Allow DNS so package managers can resolve CDN hostnames
    iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

    iptables -A OUTPUT -j REJECT --reject-with icmp-net-unreachable
    iptables -A INPUT -j REJECT --reject-with icmp-port-unreachable
fi

# --- Git configuration ---
git config --global --add safe.directory '*'
git config --global user.name "po-agent"
git config --global user.email "po-agent@localhost"

# --- Claude onboarding bypass + auth credentials ---
# Must run here (not Dockerfile) because --tmpfs /home/agent wipes the home dir
mkdir -p /home/agent/.claude
echo '{"hasCompletedOnboarding":true}' > /home/agent/.claude.json

# Copy auth credentials from host config (mounted read-only at staging path)
if [ -d /home/agent/.claude-host ]; then
    cp -a /home/agent/.claude-host/. /home/agent/.claude/
fi

chown -R agent:agent /home/agent/.claude /home/agent/.claude.json

# --- Environment ---
export CLAUDE_CONFIG_DIR="/home/agent/.claude"
export NODE_OPTIONS="--max-old-space-size=4096"
export USE_BUILTIN_RIPGREP=0

exec su-exec agent "$@"
