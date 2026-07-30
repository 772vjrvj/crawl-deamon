# -*- coding: utf-8 -*-
"""
crawl-deamon 로깅 유틸

- 일자별 로그 파일 생성
- 30일 등 보관기간이 지난 로그 정리
- 콘솔의 대기 상태를 같은 줄에 갱신
"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TextIO


class DailyFileHandler(logging.Handler):
    """
    날짜가 바뀌면 자동으로 새 로그 파일을 사용하는 Handler.

    파일명:
        logs/crawl-daemon-2026-07-30.log
    """

    def __init__(
            self,
            log_dir: Path,
            encoding: str = "utf-8",
    ):
        super().__init__()

        self.log_dir = log_dir
        self.encoding = encoding
        self.current_date = ""
        self.stream: Optional[TextIO] = None

        self.log_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    def _open_stream_if_needed(self) -> None:
        today = datetime.now().strftime(
            "%Y-%m-%d"
        )

        if self.stream and self.current_date == today:
            return

        if self.stream:
            self.stream.close()

        log_path = (
                self.log_dir
                / f"crawl-daemon-{today}.log"
        )

        self.stream = log_path.open(
            mode="a",
            encoding=self.encoding,
        )
        self.current_date = today

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._open_stream_if_needed()

            if not self.stream:
                return

            message = self.format(record)

            self.stream.write(message + "\n")
            self.stream.flush()

        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
        finally:
            super().close()


class ConsoleStatus:
    """콘솔 한 줄을 덮어쓰는 대기 상태 표시기."""

    def __init__(self) -> None:
        self.active = False
        self.last_width = 0

    def update(self, message: str) -> None:
        text = str(message)
        width = max(
            self.last_width,
            len(text),
        )

        sys.stdout.write(
            "\r" + text.ljust(width)
        )
        sys.stdout.flush()

        self.active = True
        self.last_width = width

    def finish(self) -> None:
        if not self.active:
            return

        sys.stdout.write("\n")
        sys.stdout.flush()

        self.active = False
        self.last_width = 0


def cleanup_old_logs(
        log_dir: Path,
        retention_days: int,
) -> None:
    """보관기간이 지난 일별 로그 파일을 삭제한다."""
    if retention_days <= 0:
        return

    cutoff = datetime.now() - timedelta(
        days=retention_days
    )

    for path in log_dir.glob(
            "crawl-daemon-*.log"
    ):
        try:
            modified_at = datetime.fromtimestamp(
                path.stat().st_mtime
            )

            if modified_at < cutoff:
                path.unlink(
                    missing_ok=True
                )

        except Exception:
            # 로그 정리 실패가 데몬 실행을 막으면 안 된다.
            pass


def setup_logger(
        project_root: Path,
        retention_days: int = 30,
) -> logging.Logger:
    """콘솔과 일별 파일 로그를 함께 사용하는 logger를 만든다."""
    log_dir = project_root / "logs"

    cleanup_old_logs(
        log_dir=log_dir,
        retention_days=retention_days,
    )

    logger = logging.getLogger(
        "crawl_daemon"
    )
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # main.py가 다시 로드되어도 Handler가 중복되지 않도록 초기화한다.
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(
        sys.stdout
    )
    console_handler.setFormatter(
        formatter
    )

    file_handler = DailyFileHandler(
        log_dir=log_dir,
    )
    file_handler.setFormatter(
        formatter
    )

    logger.addHandler(
        console_handler
    )
    logger.addHandler(
        file_handler
    )

    return logger