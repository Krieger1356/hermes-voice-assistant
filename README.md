# Hermes Voice Assistant

> Push-to-talk voice assistant for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Hold `Ctrl+Shift+Space`, speak, release — Hermes answers in voice.

**Windows client + Linux server.** Runs in the system tray, completely in the background. All components are free, local, and open-source — no cloud subscriptions, no API costs beyond your existing LLM provider.

![Architecture](docs/architecture.png)

## How It Works

```
Windows laptop                          Linux server (Debian)
─────────────                           ─────────────────────
Hold Ctrl+Shift+Space
  → Record WAV (16kHz)    ──── SSH ───→  faster-whisper (STT)
                                         ↓ text
                                         Hermes Agent (:8642)
                                         ↓ response
  ← Play MP3              ←─── SCP ───   Edge TTS (voice)
```

1. **Press & hold** `Ctrl+Shift+Space` (rising chime)
2. **Speak** your question
3. **Release** the hotkey (falling chime)
4. Audio is sent to your Linux server via SSH
5. Hermes Agent processes the query
6. Response is spoken back through your laptop speakers

## Features

- 🎙️ **Push-to-talk** — hold a hotkey, speak, release
- 🖥️ **System tray** — runs invisibly, green "H" icon in notification area
- 🔇 **Completely silent** — no CMD windows flash, no media player popups
- 🎤 **Windows default mic** — uses whatever mic Windows is set to
- 🔁 **Auto-retry** — waits for mic if USB headset isn't plugged in yet
- 📝 **Debug log** — everything logged to `%TEMP%\hermes-voice.log`
- 💰 **$0/month** — all components are free (faster-whisper, Edge TTS)

## Prerequisites

### Server (Linux)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed and running
- API server enabled on port 8642 (default)
- Python 3.10+
- `faster-whisper` + `edge-tts`:
  ```bash
  pip install faster-whisper edge-tts
  ```

### Client (Windows)
- Windows 10 or 11
- Python 3.10+
- OpenSSH Client (Settings → Apps → Optional Features)
- SSH key with passwordless access to your Linux server

## Quick Start

### 1. Server Setup

```bash
# Install voice pipeline dependencies
pip install faster-whisper edge-tts

# Copy the pipeline script
cp server/voice-pipeline.py ~/.hermes/scripts/
chmod +x ~/.hermes/scripts/voice-pipeline.py

# Ensure Hermes API server is running (port 8642)
hermes gateway status
```

### 2. Client Setup (Windows)

```powershell
# Install dependencies
pip install pynput sounddevice soundfile numpy pystray pillow

# Set up passwordless SSH
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh user@your-server "cat >> ~/.ssh/authorized_keys"

# Edit the script — set your server's IP
notepad client/hermes-voice-client.py
#   SERVER_HOST = "your-server-ip"
#   SERVER_USER = "your-username"

# Test it
pythonw client/hermes-voice-client.py
```

### 3. Auto-start on Boot

Create `hermes-voice.vbs` in your Startup folder (`Win+R` → `shell:startup`):
```vbs
CreateObject("Wscript.Shell").Run "pythonw ""C:\path\to\hermes-voice-client.py""", 0, False
```

Or create a shortcut to the script with Target set to:
```
pythonw "C:\path\to\hermes-voice-client.py"
```
And Run set to "Minimized".

## Configuration

Edit the top of `client/hermes-voice-client.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `SERVER_HOST` | `your-server-ip` | Your Linux server IP |
| `SERVER_USER` | `your-username` | SSH username |
| `SERVER_PIPELINE` | `~/.hermes/scripts/voice-pipeline.py` | Path to pipeline on server |
| `HOTKEY_COMBO` | `{"ctrl", "shift"}` | Modifier keys |
| `HOTKEY_KEY` | `space` | Trigger key |

Server-side environment variables (optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_VOICE_WHISPER_MODEL` | `large-v3-turbo` | Whisper model size |
| `HERMES_TTS_VOICE` | `en-IE-EmilyNeural` | TTS voice (Edge TTS) |
| `HERMES_TTS_RATE` | `+10%` | Speech rate adjustment |

## Project Structure

```
hermes-voice-assistant/
├── client/
│   └── hermes-voice-client.py    # Windows system tray + hotkey app
├── server/
│   └── voice-pipeline.py         # STT → Hermes → TTS pipeline
├── requirements-client.txt       # Python deps for Windows client
├── requirements-server.txt       # Python deps for Linux server
└── README.md
```

## Architecture Details

### Audio Playback Chain (Windows)
The client uses Windows' built-in MCI API (`winmm.dll`) for audio playback — no extra packages needed beyond what ships with Windows:
1. `mciSendString` (winmm.dll) — primary, block-until-done playback
2. `wmplayer.exe /play /close` — fallback
3. `ffplay` — if ffmpeg is in PATH
4. `os.startfile` — last resort

### Voice Pipeline (Linux)
```
WAV input → faster-whisper → text → Hermes API → response → Edge TTS → MP3 output
```

## Troubleshooting

**No audio playback:**
Check `%TEMP%\hermes-voice.log` — it logs every playback attempt with error messages.

**SSH connection fails:**
```powershell
ssh user@your-server "echo ok"
```
Should return `ok` without prompting for a password.

**Microphone not detected:**
The script auto-retries for up to 30 seconds. Check Windows sound settings — make sure your mic is set as the default recording device.

**Server pipeline fails:**
```bash
# Test the pipeline directly on the server
python3 ~/.hermes/scripts/voice-pipeline.py /tmp/test.wav
```

## License

MIT — see [LICENSE](LICENSE) file.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech-to-text
- [Edge TTS](https://github.com/rany2/edge-tts) for text-to-speech
- [pynput](https://github.com/moses-palmer/pynput) for global hotkey support
- [pystray](https://github.com/moses-palmer/pystray) for system tray integration
