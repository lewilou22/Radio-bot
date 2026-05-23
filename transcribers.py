"""Speech-to-text backends: local faster-whisper or cloud APIs (Groq / OpenAI) for mobile."""

from __future__ import annotations

import os
from typing import Protocol

import requests

from config import MODEL_SIZE, VAD_FILTER, WHISPER_COMPUTE_TYPE, WHISPER_DEVICE


class Transcriber(Protocol):
    def transcribe_path(self, path: str) -> str:
        """Return full transcript text for one audio file (e.g. MP3 chunk)."""
        ...


class FasterWhisperTranscriber:
    """Local inference (desktop / server). Each instance loads its own model."""

    def __init__(self) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            MODEL_SIZE,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )

    def transcribe_path(self, path: str) -> str:
        segments, _ = self._model.transcribe(path, vad_filter=VAD_FILTER, beam_size=1)
        return " ".join(s.text.strip() for s in segments if s.text and s.text.strip())


class GroqAudioTranscriber:
    """Groq Whisper-compatible HTTP API (good for Android APK — no heavy native libs)."""

    _URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = (api_key or os.getenv("GROQ_API_KEY") or "").strip()

    def transcribe_path(self, path: str) -> str:
        name = os.path.basename(path) or "chunk.mp3"
        mime = "audio/mpeg"
        low = name.lower()
        if low.endswith(".wav"):
            mime = "audio/wav"
        elif low.endswith((".mp4", ".m4a")):
            mime = "audio/mp4"
        with open(path, "rb") as f:
            r = requests.post(
                self._URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": (name, f, mime)},
                data={"model": "whisper-large-v3-turbo"},
                timeout=120,
            )
        r.raise_for_status()
        data = r.json()
        return (data.get("text") or "").strip()


class OpenAIAudioTranscriber:
    _URL = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = (api_key or os.getenv("OPENAI_API_KEY") or "").strip()

    def transcribe_path(self, path: str) -> str:
        name = os.path.basename(path) or "chunk.mp3"
        mime = "audio/mpeg"
        low = name.lower()
        if low.endswith(".wav"):
            mime = "audio/wav"
        with open(path, "rb") as f:
            r = requests.post(
                self._URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": (name, f, mime)},
                data={"model": "whisper-1"},
                timeout=120,
            )
        r.raise_for_status()
        data = r.json()
        return (data.get("text") or "").strip()


def make_shared_transcriber_for_backend(backend: str) -> Transcriber | None:
    """Return one shared transcriber for API modes; None means each worker uses local faster-whisper."""
    b = (backend or "").strip().lower()
    gk = (os.getenv("GROQ_API_KEY") or "").strip()
    ok = (os.getenv("OPENAI_API_KEY") or "").strip()
    if b == "groq" and gk:
        return GroqAudioTranscriber(gk)
    if b == "openai" and ok:
        return OpenAIAudioTranscriber(ok)
    if b in ("api", "cloud", "remote"):
        if gk:
            return GroqAudioTranscriber(gk)
        if ok:
            return OpenAIAudioTranscriber(ok)
    return None


def default_transcribe_backend() -> str:
    """TRANSCRIBE_BACKEND env: local | groq | openai | api (api = groq or openai if keys set)."""
    from config import default_transcribe_backend_name

    return default_transcribe_backend_name()
