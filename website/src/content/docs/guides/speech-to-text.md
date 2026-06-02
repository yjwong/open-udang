---
title: Speech-to-Text
description: Send voice messages and have them automatically transcribed.
sidebar:
  order: 10
---

OpenShrimp can transcribe voice messages and video notes using Moonshine STT, a lightweight on-device speech recognition model. Send a voice message and the transcribed text is sent to the agent as your prompt.

## How it works

1. You send a voice message or video note in Telegram
2. OpenShrimp downloads the audio (OGG/Opus format)
3. The Moonshine STT binary transcribes it locally
4. The transcribed text is sent to the agent as your message

No external services or API calls — everything runs on your machine.

## Setup

Moonshine STT is included with OpenShrimp but the binary is downloaded on first use. No manual setup is needed.

### Automatic download

The first time you send a voice message, OpenShrimp:

1. Detects that the `moonshine-stt` binary isn't installed
2. Downloads it from GitHub releases to `~/.local/share/openshrimp/bin/moonshine-stt`
3. Downloads the ONNX model files
4. Transcribes your message

Subsequent voice messages use the cached binary and models.

### Supported platforms

| Platform | Architecture | Supported |
|----------|-------------|-----------|
| Linux | x86_64 | Yes |
| Linux | aarch64 | Yes |
| macOS | Apple Silicon (aarch64) | Yes |

## The Moonshine model

Moonshine is a small, fast speech recognition model optimized for on-device inference:

- **Moonshine V1** — four ONNX model files
- **ONNX Runtime** — no GPU required, runs on CPU
- **Input** — any audio format (converted to 16kHz mono float32 PCM via PyAV)
- **Output** — plain text transcription

Models are automatically downloaded from the sherpa-onnx releases on first use.

## Limitations

- English only (Moonshine V1)
- Best with clear speech in low-noise environments
- Very long voice messages may take a few seconds to transcribe
- The first transcription is slower due to model loading

## Using voice messages effectively

Voice messages are transcribed and sent to the configured model as regular text prompts. Some tips:

- Speak clearly and at a normal pace
- Describe your request as you would type it
- You can send follow-up voice messages — they continue the same conversation
- Voice messages work in both private chats and forum topics
