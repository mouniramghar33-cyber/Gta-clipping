"""Whisper transcription (faster-whisper / CTranslate2) with word timestamps."""
from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float


@dataclass
class Transcript:
    language: str
    words: list[Word]
    segments: list[tuple[float, float, str]]

    @property
    def text(self) -> str:
        return " ".join(s[2].strip() for s in self.segments).strip()


class Transcriber:
    _lock = threading.Lock()

    def __init__(self, model: str, device: str, compute_type: str, download_root: str):
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info("loading whisper model %s (%s/%s)", self.model_name, self.device, self.compute_type)
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type,
                                       download_root=self.download_root)
        return self._model

    def transcribe(self, audio_path: str, language: str | None = None, beam_size: int = 5) -> Transcript:
        with self._lock:  # one inference at a time; the model already uses all cores
            model = self._load()
            # Decode with FFmpeg ourselves (16 kHz mono float32) instead of relying on PyAV.
            raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", audio_path,
                                  "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                                 capture_output=True, timeout=300, check=True).stdout
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            segs, info = model.transcribe(
                audio, language=language, beam_size=beam_size, word_timestamps=True,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
                condition_on_previous_text=False,
            )
            words: list[Word] = []
            segments: list[tuple[float, float, str]] = []
            for s in segs:
                segments.append((float(s.start), float(s.end), s.text))
                for w in s.words or []:
                    t = w.word.strip()
                    if t:
                        words.append(Word(float(w.start), float(w.end), t, float(w.probability)))
        return Transcript(info.language, words, segments)
