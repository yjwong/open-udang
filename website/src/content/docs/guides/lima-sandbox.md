---
title: Lima Sandbox
description: macOS VM isolation via Apple Virtualization.framework using Lima.
sidebar:
  order: 5
---

The Lima sandbox runs Claude inside a full virtual machine on macOS, using Apple's Virtualization.framework. Like the Libvirt sandbox, it provides strong isolation — the agent has no access to your host filesystem beyond the shared project directory.

## Requirements

- **macOS 13 (Ventura) or later** — Lima is not available on Linux
- No manual installation needed — OpenShrimp downloads `limactl` automatically on first use

## Basic setup

```yaml
contexts:
  myproject:
    directory: /Users/you/Documents/myproject
    description: "My project"
    allowed_tools:
      - LSP
    sandbox:
      backend: lima
```

On first use, OpenShrimp will:

1. Download `limactl` to `~/Library/Application Support/openshrimp/bin/`
2. Download an Ubuntu 24.04 cloud image
3. Create and boot a VM (takes ~30 seconds on first boot)
4. Install the Claude CLI inside the VM

Subsequent messages reuse the running VM with no boot delay.

## VM configuration

```yaml
contexts:
  myproject:
    sandbox:
      backend: lima
      memory: 4096        # MB (default: 2048) — ceiling, unused memory returned to host
      cpus: 4             # vCPUs (default: 2)
      disk_size: 40       # GB (default: 20)
```

Memory uses free-page-reporting, so the VM only consumes what it actually needs.

## Provisioning

Run a shell script on first boot to install tools and dependencies:

```yaml
contexts:
  myproject:
    sandbox:
      backend: lima
      provision: |
        apt-get update
        apt-get install -y nodejs npm golang
        npm install -g typescript
```

The provision script runs via cloud-init on the first boot. If you change the provision script or any sandbox config field, OpenShrimp detects the change and automatically rebuilds the VM.

## Additional directories

Extra host directories are shared into the VM via VirtioFS:

```yaml
contexts:
  myproject:
    directory: /Users/you/Documents/myproject
    additional_directories:
      - /Users/you/Documents/shared-lib
    sandbox:
      backend: lima
```

Both directories are available at their original paths inside the VM.

## File uploads

When you send files to the bot (photos, documents), they're copied into the VM via `limactl copy` and placed in `/tmp/openshrimp-uploads`. Claude can then read and work with them.

## Performance

- **Cold boot**: ~30 seconds. VMs are kept running between sessions for speed.
- **VirtioFS**: Near-native performance for most operations. Metadata-heavy operations (e.g. `npm install`) may be ~1.7x slower than native.

:::tip
VMs stay running between conversations. There's no boot delay for follow-up messages — only the first message after starting OpenShrimp incurs the startup cost.
:::

## macOS guest VMs

Lima can also run macOS as the guest OS — useful for testing macOS-specific workflows or running computer use with native macOS apps. This requires an Apple Silicon (ARM) host.

```yaml
contexts:
  macos-dev:
    directory: /Users/you/Documents/myproject
    description: "macOS sandbox"
    sandbox:
      backend: lima
      guest_os: macos
```

On first boot, the VM downloads a macOS IPSW restore image and installs Homebrew automatically. This initial setup takes significantly longer than a Linux guest.

### Computer use on macOS guests

macOS guests support `computer_use: true` with native macOS input methods:

```yaml
contexts:
  macos-dev:
    directory: /Users/you/Documents/myproject
    sandbox:
      backend: lima
      guest_os: macos
      computer_use: true
```

This provisions the VM with:
- **Screen Sharing** (macOS built-in VNC) for screenshots and the VNC viewer
- **Google Chrome** and **Playwright MCP** via Homebrew
- **Accessibility permissions** for input injection via `osascript` and `CGEvent`

Screenshots use `screencapture`, clicks and keyboard input use `CGEvent` (via Python), and clipboard uses `pbcopy`/`pbpaste` — all native macOS APIs.

### macOS guest limitations

- **ARM hosts only** — Lima macOS guests require Apple Silicon (M1/M2/M3/M4)
- **Slow first boot** — downloading the IPSW image and installing Homebrew + Xcode CLT can take several minutes
- **VirtioFS mount fixup** — mount symlinks may need a retry on first boot (handled automatically)

## Limitations

- **macOS only** — the Lima backend uses Apple Virtualization.framework, which is not available on Linux. Use the [Docker](/guides/docker-sandbox/) or [Libvirt](/guides/vm-sandbox/) sandbox on Linux.
