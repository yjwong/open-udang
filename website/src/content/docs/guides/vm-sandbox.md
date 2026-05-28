---
title: VM Sandbox
description: Full VM isolation with Libvirt/QEMU for maximum security.
sidebar:
  order: 4
---

The Libvirt/QEMU sandbox runs Claude inside a full virtual machine. This provides the strongest isolation — the agent has no access to your host filesystem beyond the shared project directory.

## Prerequisites

- `libvirt` and `qemu-kvm` installed and running
- The `libvirt-python` optional dependency: `uv pip install open-shrimp[libvirt]`
- `virtiofsd` for high-performance directory sharing (recommended)
- Your user in the `libvirt` group

## Basic setup

```yaml
contexts:
  myproject:
    directory: /home/you/Documents/myproject
    description: "My project"
    allowed_tools:
      - LSP
    sandbox:
      backend: libvirt
```

## How it works

1. **Base image** — OpenShrimp downloads an Ubuntu cloud image on first use (or uses a custom `base_image`)
2. **Overlay disk** — A qcow2 copy-on-write overlay is created for each context, preserving the base image
3. **Cloud-init** — SSH keys and provisioning scripts are injected via cloud-init
4. **Directory sharing** — Your project directory is shared into the VM via virtiofs (preferred) or 9p
5. **SSH connection** — OpenShrimp connects via SSH to run the Claude CLI inside the VM

## VM configuration

```yaml
contexts:
  myproject:
    sandbox:
      backend: libvirt
      memory: 4096        # MB (default: 2048) — ceiling, unused memory returned to host
      cpus: 4             # vCPUs (default: 2)
      disk_size: 40       # GB (default: 20) — qcow2 overlay max size
```

Memory uses free-page-reporting, so the VM only consumes what it actually needs — the configured value is a ceiling.

## Custom base image

Use your own qcow2 or cloud image:

```yaml
contexts:
  myproject:
    sandbox:
      backend: libvirt
      base_image: /path/to/my-custom-image.qcow2
```

## Provisioning

Run a shell script on first boot to install tools and dependencies:

```yaml
contexts:
  myproject:
    sandbox:
      backend: libvirt
      provision: |
        apt-get update
        apt-get install -y nodejs npm golang
        npm install -g typescript
```

The provision script runs via cloud-init on the first boot. If you change the provision script or toggle `computer_use`, OpenShrimp detects the change and automatically rebuilds the VM.

## Shared directories

Your project directory and any `additional_directories` are shared into the VM:

```yaml
contexts:
  myproject:
    directory: /home/you/Documents/myproject
    additional_directories:
      - /home/you/Documents/shared-lib
    sandbox:
      backend: libvirt
```

OpenShrimp starts a `virtiofsd` instance for each shared directory. Inside the VM, they're mounted at their original host paths.

## Performance

- **Cold boot**: ~13 seconds. VMs are kept running between sessions for speed.
- **SSH wait**: Up to 60 seconds on normal boot, 90 seconds after a rebuild.
- **Graceful shutdown**: 180-second timeout before force-kill on `stop()`.

:::tip
VMs stay running between conversations. There's no boot delay for follow-up messages — only the first message after starting OpenShrimp incurs the startup cost.
:::

## Computer use

Enable a desktop environment inside the VM:

```yaml
contexts:
  myproject:
    sandbox:
      backend: libvirt
      computer_use: true
```

The VM sandbox uses QEMU's QMP (QEMU Machine Protocol) for mouse, keyboard, and scroll input, and the libvirt screenshot API for capturing the screen. A VNC port is auto-assigned and accessible via the `/vnc` command.

See the [Computer Use](/guides/computer-use/) guide for details.

## Troubleshooting

### VM won't start

Check that libvirt is running:

```bash
systemctl status libvirtd
```

Verify your user is in the libvirt group:

```bash
groups | grep libvirt
```

### SSH connection fails

If SSH fails after boot, OpenShrimp automatically rebuilds the VM. Check the serial log in the sandbox state directory for boot errors.

### virtiofsd not available

If virtiofsd isn't installed, OpenShrimp falls back to 9p directory sharing, which is slower but functional.
