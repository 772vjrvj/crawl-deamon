# -*- coding: utf-8 -*-
"""GUI 없이 실행되는 데몬 Worker 공통 기능."""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Dict, Optional


class NullSignal:
    """기존 GUI Worker의 emit 호출을 안전하게 흡수한다."""

    def emit(self, *_args: Any, **_kwargs: Any) -> None:
        return


class DaemonBaseWorker:
    """기존 BaseApiWorker 중 데몬에 필요한 기능만 제공한다."""

    def __init__(
            self,
            setting: Optional[Dict[str, Any]] = None,
            logger: Any = None,
    ) -> None:
        self.setting: Dict[str, Any] = dict(setting or {})
        self.logger = logger
        self.running = True
        self.progress_signal = NullSignal()
        self.progress_end_signal = NullSignal()

    def log_signal_func(self, message: str) -> None:
        if self.logger:
            self.logger.info("%s", message)
        else:
            print(message)

    @staticmethod
    def get_setting_value(setting: Any, code_name: str) -> Optional[Any]:
        if isinstance(setting, dict):
            return setting.get(code_name)

        if isinstance(setting, list):
            for item in setting:
                if isinstance(item, dict) and item.get("code") == code_name:
                    return item.get("value")

        return None

    def sleep_s(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            if not self.running:
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return self.running

    @staticmethod
    def get_project_root() -> str:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def get_resource_root(self) -> str:
        # --onedir + --contents-directory . 빌드에서도 실행 파일 폴더가 기준이다.
        return self.get_project_root()


ResultInserter = Callable[[str, Dict[str, Any], Optional[str]], int]
ExistingStoreLoader = Callable[[], set[str]]
