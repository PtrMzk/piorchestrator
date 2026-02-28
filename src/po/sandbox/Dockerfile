FROM node:22-alpine

RUN apk add --no-cache \
    git bash iptables su-exec \
    ripgrep libgcc libstdc++ \
    python3 py3-pip

RUN pip install --break-system-packages uv
RUN npm install -g @anthropic-ai/claude-code

RUN adduser -D -h /home/agent agent

# Skip interactive onboarding (critical for headless use)
RUN mkdir -p /home/agent/.claude && \
    echo '{"hasCompletedOnboarding":true}' > /home/agent/.claude.json && \
    chown -R agent:agent /home/agent/.claude /home/agent/.claude.json

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
