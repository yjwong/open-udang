---
title: systemd Service
description: Run OpenShrimp as a systemd user service on Linux.
sidebar:
  order: 1
---

OpenShrimp includes a built-in installer that creates a systemd user service (Linux) or launchd agent (macOS).

## Automatic installation

The easiest way to install the service:

```bash
openshrimp install
```

This will:
1. Detect your platform (Linux or macOS)
2. Find the `openshrimp` executable
3. Generate and write the service file
4. Enable and start the service
5. Enable login lingering (so the service runs without an active login session)

The service file is written to `~/.config/systemd/user/open-shrimp.service`.

## Manual installation

If you prefer to create the service file manually:

```ini
[Unit]
Description=OpenShrimp Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/path/to/openshrimp --config /home/you/.config/openshrimp/config.yaml
Restart=on-failure
RestartSec=5
# Optional: provider-specific credentials if you do not use /connect
Environment=OPENAI_API_KEY=sk-...

[Install]
WantedBy=default.target
```

Save this to `~/.config/systemd/user/open-shrimp.service`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable open-shrimp
systemctl --user start open-shrimp
```

### Using an environment file

Instead of putting provider credentials directly in the unit file, you can use an environment file:

```bash
echo 'OPENAI_API_KEY=sk-...' > ~/.config/openshrimp/.env
chmod 600 ~/.config/openshrimp/.env
```

Then add to the `[Service]` section:

```ini
EnvironmentFile=/home/you/.config/openshrimp/.env
```

## Login lingering

By default, systemd user services stop when you log out. Enable lingering to keep the service running:

```bash
loginctl enable-linger
```

The automatic installer does this for you.

## Useful commands

```bash
systemctl --user status open-shrimp    # check status
journalctl --user -u open-shrimp -f    # follow logs
systemctl --user restart open-shrimp   # restart
systemctl --user stop open-shrimp      # stop
```

## macOS (launchd)

On macOS, `openshrimp install` creates a launchd user agent at `~/Library/LaunchAgents/com.openshrimp.bot.plist`. Logs are written to `~/Library/Logs/OpenShrimp/`.

```bash
launchctl list | grep com.openshrimp    # check status
tail -f ~/Library/Logs/OpenShrimp/openshrimp.stderr.log  # follow logs
```

## Uninstalling

```bash
openshrimp uninstall
```

This stops, disables, and removes the service file.
