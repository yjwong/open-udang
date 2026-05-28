---
title: Docker Sandbox
description: Run Claude in an isolated Docker container with filesystem safety.
sidebar:
  order: 3
---

The Docker sandbox runs the Claude CLI inside a Linux container. The project directory is bind-mounted in, but the agent can't access anything else on your host. This means you can safely auto-approve all tool calls — the sandbox provides the safety boundary.

## Basic setup

```yaml
contexts:
  myproject:
    directory: /home/you/Documents/myproject
    description: "My project"
    allowed_tools:
      - LSP
    sandbox:
      backend: docker
```

That's it. OpenShrimp handles building the image and starting the container.

## What happens under the hood

1. **Image build** — On first use, OpenShrimp builds a Docker image based on the default `openshrimp-claude` base image. This is cached and reused.
2. **Container start** — The container runs with your project directory bind-mounted at the same path. It runs as your host uid/gid.
3. **CLI wrapper** — A shell wrapper script is generated that runs `docker exec` into the container, forwarding the Claude CLI args and your `ANTHROPIC_API_KEY`.
4. **Auto-approval** — All Bash commands and path-scoped tools are auto-approved since the sandbox isolates the filesystem.

## Custom Dockerfile

Install project-specific toolchains by providing a custom Dockerfile:

```yaml
contexts:
  myproject:
    directory: /home/you/Documents/myproject
    description: "My project"
    allowed_tools:
      - LSP
    sandbox:
      backend: docker
      dockerfile: /home/you/Documents/myproject/Dockerfile.claude
```

The Dockerfile should extend the base image:

```dockerfile
FROM openshrimp-claude:latest

# Install Node.js
RUN apt-get update && apt-get install -y nodejs npm

# Install project-specific tools
RUN npm install -g typescript
```

The image is tagged as `openshrimp-claude:<context-name>` and built lazily on first use. The build context is the Dockerfile's parent directory.

## Docker-in-Docker

Enable rootless Docker inside the container for projects that need to build or run containers:

```yaml
contexts:
  myproject:
    sandbox:
      backend: docker
      docker_in_docker: true
```

This starts a rootless Docker daemon inside the container (with `--cap-add SYS_ADMIN`). The agent can then run `docker build`, `docker run`, `docker compose`, etc. The host Docker socket is **not** passed through.

:::caution
Docker-in-Docker reduces isolation. Only enable this for projects that genuinely need it.
:::

## Additional directories

When your context has `additional_directories`, those are also bind-mounted into the container:

```yaml
contexts:
  myproject:
    directory: /home/you/Documents/myproject
    additional_directories:
      - /home/you/Documents/shared-lib
    sandbox:
      backend: docker
```

Both directories are available at their original paths inside the container.

## File uploads

When you send files to the bot (photos, documents), they're copied into the container via `docker cp` and placed in a temporary upload directory. Claude can then read and work with them.

## Session storage

Each sandboxed context has its own isolated session storage under `~/.local/share/openshrimp/containers/<context>/`. This keeps Claude's session files separate from your host's Claude data.

## Computer use

Enable a headless desktop inside the container for GUI interaction:

```yaml
contexts:
  myproject:
    sandbox:
      backend: docker
      computer_use: true
```

See the [Computer Use](/guides/computer-use/) guide for details.

## Requirements

- Docker installed and accessible to your user (no sudo needed)
- Sufficient disk space for the container image (~1-2 GB for the base image)
