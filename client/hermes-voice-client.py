#!/usr/bin/env python3
"""
Hermes Voice Assistant — Windows Push-to-Talk Client (System Tray)
===================================================================
Hold Ctrl+Shift+Space, speak, release — Hermes answers in voice.

Runs in the system tray — no terminal window needed.
Uses Windows default recording device. Restart if you switch mics.

Requires:
    pip install pynput sounddevice soundfile numpy pystray pillow

Audio playback uses Windows built-in MCI API — no extra packages needed.

Quick setup:
    1. pip install pynput sounddevice soundfile numpy pystray pillow
    2. Enable OpenSSH Client (Settings → Apps → Optional Features)
    3. ssh-keygen -t ed25519 && type ~/.ssh/id_ed25519.pub | ssh user@host "cat >> ~/.ssh/authorized_keys"
    4. Edit SERVER_HOST / SERVER_USER below
    5. Run: pythonw hermes-voice-client.py

Auto-start on boot:
    Win+R → shell:startup → create hermes-voice.bat:
        @echo off
        start "" /MIN pythonw "C:/Users/YourName/hermes-voice-client.py"
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────
SERVER_HOST = "your-server-ip"        # Your Linux server IP or hostname
SERVER_USER = "your-username"         # SSH username on the server
SERVER_PIPELINE = "~/.hermes/scripts/voice-pipeline.py"
HOTKEY_COMBO = {"ctrl", "shift"}    # Hold Ctrl+Shift then press Space
HOTKEY_KEY = "space"                # The key you press while holding modifiers
SAMPLE_RATE = 16000
SSH_TIMEOUT = 120
LOG_FILE = os.path.join(tempfile.gettempdir(), "hermes-voice.log")
# Prevent subprocess calls from flashing CMD windows
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
# ────────────────────────────────────────────────────────────────────

# PTT state
recording = False
audio_frames = []
tray_icon = None


def log(msg: str):
    """Write to log file (no console, since we run with pythonw)."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify_tray(msg: str):
    """Show a system tray notification bubble."""
    global tray_icon
    if tray_icon and hasattr(tray_icon, "notify"):
        try:
            tray_icon.notify(msg, "Hermes Voice")
        except Exception:
            pass


# ── Audio playback ─────────────────────────────────────────────────

def play_audio(filepath: str):
    """Play an MP3 audio file. Uses Windows built-in APIs only."""
    fp = Path(filepath)
    log(f"play_audio: {filepath} (exists={fp.exists()}, size={fp.stat().st_size if fp.exists() else 'N/A'})")

    if not fp.exists():
        log("play_audio: file not found!")
        notify_tray("Audio file missing")
        return

    # Method 1: mciSendString — Windows multimedia API since Win95
    # Works with MP3 on Win10/11. 'wait' flag blocks until playback completes.
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        abs_path = str(fp.resolve())
        # Double the string buffer for mciSendString (needs space for return)
        buf = ctypes.create_unicode_buffer(256)
        
        # Open
        r = winmm.mciSendStringW(f'open "{abs_path}" type mpegvideo alias hva', buf, 256, None)
        if r == 0:
            log("play_audio: MCI open OK")
            # Get length in ms
            winmm.mciSendStringW('status hva length', buf, 256, None)
            length_ms = int(buf.value) if buf.value else 30000
            log(f"play_audio: MCI length={length_ms}ms")
            
            # Play with wait — blocks until done
            winmm.mciSendStringW(f'play hva wait', None, 0, None)
            log("play_audio: MCI playback complete")
            
            # Close
            winmm.mciSendStringW('close hva', None, 0, None)
            return
        else:
            winmm.mciGetErrorStringW(r, buf, 256)
            log(f"play_audio: MCI failed ({r}): {buf.value}")
    except Exception as e:
        log(f"play_audio: MCI error: {e}")

    # Method 2: Windows Media Player command-line (built-in)
    # /play starts playback, /close exits after
    try:
        log("play_audio: trying wmplayer.exe...")
        subprocess.run(
            ["wmplayer", "/play", "/close", str(fp.resolve())],
            capture_output=True, timeout=60, creationflags=NO_WINDOW
        )
        log("play_audio: wmplayer completed")
        return
    except FileNotFoundError:
        log("play_audio: wmplayer.exe not found")
    except subprocess.TimeoutExpired:
        log("play_audio: wmplayer timed out")
    except Exception as e:
        log(f"play_audio: wmplayer error: {e}")

    # Method 3: ffplay (if ffmpeg is in PATH)
    try:
        log("play_audio: trying ffplay...")
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(fp)],
            capture_output=True, timeout=60, creationflags=NO_WINDOW
        )
        log("play_audio: ffplay completed")
        return
    except FileNotFoundError:
        log("play_audio: ffplay not found")
    except Exception as e:
        log(f"play_audio: ffplay error: {e}")

    # Method 4: open with default player (last resort)
    log("play_audio: falling back to os.startfile")
    notify_tray("Playing in media player...")
    os.startfile(filepath)


def play_chime():
    """Play a short beep for recording start/stop."""
    import numpy as np
    import sounddevice as sd

    duration = 0.08
    t = np.linspace(0, duration, int(SAMPLE_RATE * duration), False)
    freq = 880 if recording else 440
    tone = np.sin(2 * np.pi * freq * t) * 0.3
    try:
        sd.play(tone, SAMPLE_RATE)
        sd.wait()
    except Exception as e:
        log(f"play_chime error: {e}")


# ── Recording ──────────────────────────────────────────────────────

def record_callback(indata, frames, time_info, status):
    if recording:
        audio_frames.append(indata.copy())


# ── Hotkey handler ───────────────────────────────────────────────

def on_press(key):
    global recording
    try:
        from pynput.keyboard import Key

        is_trigger = False
        if hasattr(key, "char") and key.char:
            if key.char.lower() == HOTKEY_KEY.lower():
                is_trigger = True
        elif key == getattr(Key, HOTKEY_KEY, None):
            is_trigger = True
        if not is_trigger:
            return

        # Check modifiers
        import ctypes
        user32 = ctypes.windll.user32
        if "ctrl" in HOTKEY_COMBO and not (user32.GetAsyncKeyState(0x11) & 0x8000):
            return
        if "shift" in HOTKEY_COMBO and not (user32.GetAsyncKeyState(0x10) & 0x8000):
            return

        if not recording:
            recording = True
            audio_frames.clear()
            threading.Thread(target=play_chime, daemon=True).start()
            log("Recording started")
            notify_tray("🎙️ Listening...")
    except Exception as e:
        log(f"on_press error: {e}")


def on_release(key):
    global recording
    try:
        from pynput.keyboard import Key

        is_trigger = False
        if hasattr(key, "char") and key.char:
            if key.char.lower() == HOTKEY_KEY.lower():
                is_trigger = True
        elif key == getattr(Key, HOTKEY_KEY, None):
            is_trigger = True

        if is_trigger and recording:
            recording = False
            threading.Thread(target=play_chime, daemon=True).start()
            log("Recording stopped — processing")
            threading.Thread(target=process_recording, daemon=True).start()
    except Exception as e:
        log(f"on_release error: {e}")


# ── Server communication ───────────────────────────────────────────

def scp_upload(local: str, remote: str) -> bool:
    dest = f"{SERVER_USER}@{SERVER_HOST}:{remote}"
    try:
        r = subprocess.run(
            ["scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
             local, dest],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW
        )
        if r.returncode != 0:
            log(f"SCP upload FAIL: {r.stderr}")
            return False
        return True
    except Exception as e:
        log(f"SCP upload error: {e}")
        return False


def scp_download(remote: str, local: str) -> bool:
    src = f"{SERVER_USER}@{SERVER_HOST}:{remote}"
    try:
        r = subprocess.run(
            ["scp", "-o", "ConnectTimeout=5", src, local],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW
        )
        if r.returncode != 0:
            log(f"SCP download FAIL: {r.stderr}")
            return False
        log(f"SCP download OK: {local} ({Path(local).stat().st_size} bytes)")
        return True
    except Exception as e:
        log(f"SCP download error: {e}")
        return False


def ssh_run(command: str) -> tuple:
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
             f"{SERVER_USER}@{SERVER_HOST}", command],
            capture_output=True, text=True, timeout=SSH_TIMEOUT, creationflags=NO_WINDOW
        )
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def run_pipeline(remote_wav: str) -> dict | None:
    cmd = f"python3 {SERVER_PIPELINE} {remote_wav}"
    log(f"Running pipeline: {cmd}")
    ret, stdout, stderr = ssh_run(cmd)

    for line in stderr.strip().split("\n"):
        if "[pipeline]" in line:
            log(f"  {line.strip()}")

    if ret != 0:
        log(f"Pipeline FAILED (exit {ret}): {stderr}")
        return None

    # JSON is the last line of stdout
    try:
        lines = stdout.strip().split("\n")
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        log(f"Pipeline invalid JSON: {stdout[:300]}")
        return None


# ── Main processing ────────────────────────────────────────────────

def process_recording():
    import numpy as np
    import soundfile as sf

    if not audio_frames:
        log("No audio recorded")
        return

    try:
        audio_data = np.concatenate(audio_frames, axis=0)
    except ValueError:
        log("Audio buffer empty")
        return

    local_wav = os.path.join(tempfile.gettempdir(), "hermes_ptt_input.wav")
    sf.write(local_wav, audio_data, SAMPLE_RATE)
    duration = len(audio_data) / SAMPLE_RATE
    log(f"Recorded {duration:.1f}s → {local_wav}")

    remote_wav = "/tmp/hermes_ptt_input.wav"
    if not scp_upload(local_wav, remote_wav):
        notify_tray("Failed to reach server")
        cleanup(local_wav)
        return

    result = run_pipeline(remote_wav)
    if not result:
        notify_tray("Hermes didn't respond")
        cleanup(local_wav)
        ssh_run(f"rm -f {remote_wav}")
        return

    text = result.get("text", "?")
    response = result.get("response", "?")
    log(f"You: {text}")
    log(f"Hermes: {response[:100]}...")

    remote_audio = result.get("audio", "")
    if not remote_audio:
        log("No audio in response")
        notify_tray(f"Hermes: {response[:80]}...")
        cleanup(local_wav)
        ssh_run(f"rm -f {remote_wav}")
        return

    local_audio = os.path.join(tempfile.gettempdir(), "hermes_ptt_response.mp3")
    if scp_download(remote_audio, local_audio):
        log("Playing response...")
        notify_tray("🔈 Speaking...")
        play_audio(local_audio)
        cleanup(local_audio)

    cleanup(local_wav)
    ssh_run(f"rm -f {remote_wav} {remote_audio}")


def cleanup(*paths: str):
    for p in paths:
        try:
            if os.path.exists(p):
                os.unlink(p)
        except Exception:
            pass


# ── System tray ────────────────────────────────────────────────────

def create_tray_icon():
    """Create a simple tray icon using Pillow."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Green circle for "ready"
    draw.ellipse([8, 8, 56, 56], fill=(0, 180, 80))
    # White "H" for Hermes
    draw.text((22, 18), "H", fill=(255, 255, 255))
    return img


def setup_tray():
    """Start the system tray icon and menu."""
    global tray_icon

    try:
        import pystray
    except ImportError:
        log("pystray not installed — running without system tray")
        return None

    from PIL import Image
    from pystray import Menu, MenuItem

    icon_image = create_tray_icon()

    def on_exit(icon, item):
        log("Exit requested from tray")
        icon.stop()
        os._exit(0)

    def on_status(icon, item):
        notify_tray("Hermes Voice is running\nCtrl+Shift+Space to speak")

    menu = Menu(
        MenuItem("Status", on_status, default=True),
        MenuItem("Exit", on_exit),
    )

    icon = pystray.Icon("hermes_voice", icon_image, "Hermes Voice", menu)
    tray_icon = icon
    return icon


# ── Initialization ─────────────────────────────────────────────────

def get_default_mic_info() -> str:
    import sounddevice as sd
    try:
        return sd.query_devices(kind="input")["name"]
    except Exception:
        return "Unknown"


def open_audio_stream():
    import sounddevice as sd

    for attempt in range(30):
        try:
            sd.check_input_settings(samplerate=SAMPLE_RATE, channels=1, device=None)
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1,
                callback=record_callback, dtype="float32", device=None
            )
            stream.start()
            return stream
        except (sd.PortAudioError, OSError) as e:
            delay = min(2 ** attempt, 30)
            log(f"No mic ({e}) — retry in {delay}s (attempt {attempt+1}/30)")
            time.sleep(delay)

    log("FATAL: No mic after 30 attempts")
    sys.exit(1)


def check_dependencies():
    missing = []
    for mod in ["pynput", "sounddevice", "soundfile", "numpy"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        log(f"Missing packages: {missing}")
        sys.exit(1)

    log(f"Testing SSH to {SERVER_USER}@{SERVER_HOST}...")
    ret, stdout, stderr = ssh_run("echo ok")
    if ret != 0:
        log(f"SSH FAILED: {stderr}")
        sys.exit(1)
    log("SSH OK")


# ── Entry point ────────────────────────────────────────────────────

def main():
    from pynput.keyboard import Listener

    log("=" * 40)
    log("Hermes Voice Assistant starting")
    log(f"Server: {SERVER_USER}@{SERVER_HOST}")
    log(f"Hotkey: Ctrl+Shift+Space")
    log(f"Log: {LOG_FILE}")

    check_dependencies()

    mic_name = get_default_mic_info()
    log(f"Mic: {mic_name}")

    stream = open_audio_stream()
    log("Audio stream ready")

    # Start keyboard listener in a daemon thread
    listener = Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    log("Hotkey listener started")

    # Start system tray (blocks until exit)
    icon = setup_tray()
    if icon:
        notify_tray("Hermes Voice is ready\nCtrl+Shift+Space to speak")
        log("System tray started — running...")
        try:
            icon.run()
        except KeyboardInterrupt:
            pass
    else:
        # No tray — just wait on keyboard interrupt
        log("No tray support — press Ctrl+C to exit")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    log("Shutting down")
    stream.stop()
    stream.close()
    listener.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(f"FATAL: {traceback.format_exc()}")
        sys.exit(1)
