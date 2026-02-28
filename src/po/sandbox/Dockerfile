FROM node:22-alpine

# Install system dependencies
RUN apk add --no-cache \
    git \
    iptables \
    su-exec \
    bash

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Create non-root agent user
RUN adduser -D -h /home/agent agent

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
