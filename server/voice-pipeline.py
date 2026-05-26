#!/usr/bin/env python3
"""
Hermes Voice Assistant — Server Pipeline
=========================================
Runs on the Debian server. Takes a WAV audio file, transcribes it,
sends to Hermes API, converts response to speech.

Usage:
    python3 voice-pipeline.py <input.wav> [--output-dir /path]

Output:
    Prints JSON to stdout: {"text": "...", "audio": "/path/to/output.mp3", "response": "..."}
    Audio file is saved alongside the input or in --output-dir.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────
HERMES_API_URL = os.getenv("HERMES_VOICE_API_URL", "http://localhost:8642/v1/chat/completions")
WHISPER_MODEL = os.getenv("HERMES_VOICE_WHISPER_MODEL", "large-v3-turbo")
TTS_VOICE = os.getenv("HERMES_TTS_VOICE", "en-IE-EmilyNeural")
TTS_RATE = os.getenv("HERMES_TTS_RATE", "+10%")  # Slightly faster for assistant feel
MAX_RESPONSE_CHARS = int(os.getenv("HERMES_VOICE_MAX_CHARS", "2000"))
TRANSCRIPTS_DIR = Path(os.getenv(
    "HERMES_VOICE_TRANSCRIPTS_DIR",
    str(Path.home() / ".hermes" / "voice-transcripts")
))


def transcribe(wav_path: str) -> str:
    """Transcribe WAV audio using faster-whisper."""
    import re
    from faster_whisper import WhisperModel

    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        wav_path,
        beam_size=5,
        language="en",
        vad_filter=True,  # Silero VAD — skip silence/noise regions
        # vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False,  # reduce hallucination chaining
    )
    text = " ".join(seg.text.strip() for seg in segments)

    # Strip Whisper hallucination artifacts
    # — asterisks, music notes, dash-only lines, angle brackets
    text = re.sub(r"\*+", "", text)           # *** → nothing
    text = re.sub(r"♪+", "", text)            # music notes
    text = re.sub(r"^\s*[-–—]+\s*$", "", text, flags=re.MULTILINE)  # lone dashes
    text = re.sub(r"<[^>]*>", "", text)       # <whisper artifacts>
    text = re.sub(r"\s{2,}", " ", text)       # collapse multiple spaces
    text = text.strip()

    return text


def query_hermes(text: str) -> str:
    """Send transcribed text to Hermes API and get response."""
    import urllib.request

    payload = json.dumps({
        "model": "hermes-agent",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are speaking aloud to the user via text-to-speech. "
                    "Everything you say will be read out loud — no screen, no visuals. "
                    "Rules:\n"
                    "- Use ONLY plain, natural spoken language\n"
                    "- NO markdown, NO code blocks (```), NO tables, NO ASCII diagrams\n"
                    "- NO bold/italic with asterisks or any formatting syntax\n"
                    "- NO phrases like 'as shown above', 'see below', 'in this table'\n"
                    "- Say numbers naturally: 'twenty-five' not '25'\n"
                    "- Use normal sentence capitalization, no special characters\n"
                    "- Speak conversationally — this is a spoken conversation"
                ),
            },
            {"role": "user", "content": text},
        ],
        "max_tokens": 500,
    }).encode("utf-8")

    req = urllib.request.Request(
        HERMES_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    start = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    elapsed = time.time() - start
    content = data["choices"][0]["message"]["content"]
    tokens = data.get("usage", {}).get("total_tokens", 0)
    print(f"[pipeline] Hermes responded in {elapsed:.1f}s ({tokens} tokens)", file=sys.stderr)
    return content


def synthesize(text: str, output_path: str) -> str:
    """Convert text to speech using edge-tts."""
    import asyncio
    import edge_tts

    async def _synth():
        communicate = edge_tts.Communicate(
            text[:MAX_RESPONSE_CHARS],
            TTS_VOICE,
            rate=TTS_RATE,
        )
        await communicate.save(output_path)

    asyncio.run(_synth())
    return output_path


def save_transcript(text: str, response: str, timestamp: str) -> str:
    """Save a voice conversation transcript."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    # Formatted markdown
    time_fmt = time.strftime("%Y-%m-%d %H:%M:%S")
    content = f"""\
# Voice Transcript — {time_fmt}

**You:** {text}

**Hermes:** {response}
"""

    # Timestamped file
    ts_file = TRANSCRIPTS_DIR / f"{timestamp}.md"
    ts_file.write_text(content, encoding="utf-8")

    # Always-updated latest.md for quick access
    latest = TRANSCRIPTS_DIR / "latest.md"
    latest.write_text(content, encoding="utf-8")

    return str(ts_file)


def main():
    parser = argparse.ArgumentParser(description="Hermes Voice Pipeline")
    parser.add_argument("input", help="Path to input WAV file")
    parser.add_argument("--output-dir", help="Directory for output MP3", default=None)
    args = parser.parse_args()

    wav_path = Path(args.input)
    if not wav_path.exists():
        print(json.dumps({"error": f"File not found: {args.input}"}))
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else wav_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Transcribe
    print(f"[pipeline] Transcribing with {WHISPER_MODEL}...", file=sys.stderr)
    t0 = time.time()
    text = transcribe(str(wav_path))
    print(f"[pipeline] Transcription ({time.time()-t0:.1f}s): {text}", file=sys.stderr)

    if not text.strip():
        print(json.dumps({"error": "No speech detected"}))
        sys.exit(1)

    # Step 2: Query Hermes
    print("[pipeline] Querying Hermes...", file=sys.stderr)
    response = query_hermes(text)

    # Save transcript
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    transcript_path = save_transcript(text, response, timestamp)
    print(f"[pipeline] Transcript saved: {transcript_path}", file=sys.stderr)

    # Step 3: Synthesize speech
    mp3_path = output_dir / f"hermes_response_{timestamp}.mp3"
    print(f"[pipeline] Synthesizing speech...", file=sys.stderr)
    t0 = time.time()
    synthesize(response, str(mp3_path))
    print(f"[pipeline] TTS done ({time.time()-t0:.1f}s)", file=sys.stderr)

    # Output result as JSON
    result = {
        "text": text,
        "response": response,
        "audio": str(mp3_path),
        "transcript": transcript_path,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
