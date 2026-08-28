FROM ghcr.io/nvidia/openshell-community/sandboxes/base@sha256:aeef1c63f00e2913ea002ccb3aaf925f338b5c5d70e63576f0d95c16a138044e

USER root

ARG COPILOT_VERSION=1.0.81
RUN npm install -g "@github/copilot@${COPILOT_VERSION}" \
    && copilot --version

ENV XDG_DATA_HOME=/opt/agent/share \
    XDG_CONFIG_HOME=/opt/agent/config \
    XDG_CACHE_HOME=/opt/agent/cache
RUN mkdir -p /opt/agent/share /opt/agent/config /opt/agent/cache \
    && chown -R 998:998 /opt/agent

USER 998
WORKDIR /sandbox
