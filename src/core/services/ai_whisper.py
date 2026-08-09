# ai_whisper.py
from __future__ import annotations

import threading
from typing import Any, Optional

_model: Optional[Any] = None
_lock = threading.Lock()


def init(model_size: str = "small") -> None:
    global _model

    if _model is not None:
        return

    with _lock:
        if _model is not None:
            return

        import whisper

        print("Whisper 모델 로딩 중...")
        _model = whisper.load_model(model_size)
        print("Whisper 모델 로딩 완료")


def get_model() -> Any:
    if _model is None:
        raise RuntimeError("Whisper 모델이 초기화되지 않았습니다. init()을 먼저 호출하세요.")
    return _model


def transcribe(audio_path: str, language: str = "ko") -> dict:
    model = get_model()
    return model.transcribe(audio_path, language=language, fp16=False)