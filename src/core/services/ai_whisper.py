# ai_whisper.py
from __future__ import annotations

import threading
from typing import Any, Optional

_model: Optional[Any] = None
_lock = threading.Lock()



# 모델	특징	GPU 메모리(VRAM)
# tiny	가장 빠르지만 정확도 낮음	약 1GB
# base	빠르고 가벼움	약 1GB
# small	속도와 정확도의 균형	약 2GB
# medium	small보다 일반적으로 정확하지만 느림	약 5GB
# large	높은 정확도, 느리고 메모리 사용량 큼	약 10GB
# turbo	large-v3를 빠르게 최적화한 모델	약 6GB

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