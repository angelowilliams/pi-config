# Build for the Nobara host's linux/amd64 architecture.
FROM docker.io/docker/sandbox-templates:shell-docker@sha256:53b08fa716a1725f5a238b69e85f05f96956e621943d7a5c4466502321b07cfd

USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends fd-find=10.3.0-2ubuntu1 && \
    rm -rf /var/lib/apt/lists/* && \
    ln -s /usr/bin/fdfind /usr/local/bin/fd

# Pi's release publishes a locked npm install for the CLI and its dependencies.
ADD --checksum=sha256:{{SHA256_PACKAGE_JSON}} {{RELEASE}}/pi-coding-agent-install-package.json /opt/pi/package.json
ADD --checksum=sha256:{{SHA256_PACKAGE_LOCK_JSON}} {{RELEASE}}/pi-coding-agent-install-package-lock.json /opt/pi/package-lock.json
RUN npm ci --prefix /opt/pi --ignore-scripts --no-audit --no-fund --cache /tmp/pi-npm && \
    rm -rf /tmp/pi-npm && \
    ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi

COPY --chown=agent:agent agent/ /home/agent/.pi/agent/
USER agent
WORKDIR /home/agent
RUN test "$(pi --version)" = {{PI_VERSION}} && \
    node -e 'for (const entry of require("./.pi/agent/settings.json").packages) console.log(typeof entry === "string" ? entry : entry.source)' > /tmp/pi-packages && \
    while IFS= read -r package; do \
      npm_config_cache=/tmp/pi-npm pi install "$package" || exit; \
    done < /tmp/pi-packages && \
    rm -rf /tmp/pi-packages /tmp/pi-npm

USER root
COPY privacy.ts privacy-runtime.ts privacy-models.json /opt/pi/
COPY --chown=agent:agent private-agent/ /opt/pi/private-agent/
USER agent

LABEL com.docker.sandboxes.flavor="pi"
ENTRYPOINT ["pi"]
