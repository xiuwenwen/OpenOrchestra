FROM python:3.11-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

ARG INSTALL_AGENT_CLIS=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        make \
        nodejs \
        npm \
        ripgrep \
        rsync \
        unzip \
    && python -m pip install -U pip setuptools wheel pytest coverage build tox nox \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$INSTALL_AGENT_CLIS" = "true" ]; then \
        npm install -g @openai/codex @anthropic-ai/claude-code; \
    fi

RUN useradd --create-home --shell /bin/bash openorchestra \
    && mkdir -p /workspace \
    && chown openorchestra:openorchestra /workspace

WORKDIR /workspace
USER openorchestra
ENV HOME=/home/openorchestra
