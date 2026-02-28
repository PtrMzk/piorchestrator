#!/bin/bash
set -e

# --- Network isolation ---
# Extract API IPs from /etc/hosts (injected by --add-host)
API_IPS=$(grep 'api.anthropic.com' /etc/hosts | awk '{print $1}' | sort -u)

if [ -n "$API_IPS" ]; then
    # Allow loopback
    iptables -A INPUT -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT

    # Allow established/related connections
    iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

    # Allow outbound to API IPs on port 443 only
    for ip in $API_IPS; do
        iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
    done

    # Reject everything else (REJECT fails fast; DROP causes hangs)
    iptables -A OUTPUT -j REJECT --reject-with icmp-net-unreachable
    iptables -A INPUT -j REJECT --reject-with icmp-port-unreachable
fi

# --- Git configuration ---
# Mounted volumes have different ownership; mark all dirs as safe
git config --global --add safe.directory '*'
git config --global user.name "po-agent"
git config --global user.email "po-agent@localhost"

# --- Drop privileges and exec the command ---
exec su-exec agent "$@"
