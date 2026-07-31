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
    """
    콘솔의 같은 한 줄을 계속 덮어쓰면서
    진행 상태나 대기 상태를 표시하는 클래스.

    일반적인 print()나 logger는 호출할 때마다
    새로운 줄에 출력하지만, 이 클래스는 현재 줄을 계속 갱신한다.

    사용 예:
        status = ConsoleStatus()

        status.update("처리 중... 1/5")
        status.update("처리 중... 2/5")
        status.update("처리 중... 3/5")
        status.update("처리 중... 4/5")
        status.update("처리 중... 5/5")

        # 한 줄 상태 출력을 끝내고 다음 줄로 이동
        status.finish()

        print("처리가 완료되었습니다.")

    화면에는 update()가 호출될 때마다 줄이 추가되는 것이 아니라
    같은 한 줄의 내용이 계속 변경된다.

    최종 화면 예:
        처리 중... 5/5
        처리가 완료되었습니다.
    """

    def __init__(self) -> None:
        # 현재 콘솔에 한 줄 상태 메시지가 출력되어 있는지 여부
        #
        # False:
        #   아직 update()가 호출되지 않았거나 finish()로 종료된 상태
        #
        # True:
        #   update()로 한 줄 상태 메시지가 출력 중인 상태
        self.active = False

        # 이전에 출력한 메시지의 너비
        #
        # 새 메시지가 이전 메시지보다 짧을 때
        # 이전 메시지의 남은 글자를 공백으로 지우기 위해 사용한다.
        #
        # 예:
        #   이전 메시지: "로그인 처리 중입니다..."
        #   새 메시지:   "완료"
        #
        # 공백을 채우지 않으면 이전 글자가 뒤에 남을 수 있다.
        self.last_width = 0

    def update(self, message: str) -> None:
        """
        전달받은 상태 메시지를 현재 콘솔 줄에 덮어쓴다.

        print()처럼 다음 줄로 이동하지 않고,
        같은 줄의 맨 앞으로 돌아가 메시지만 변경한다.
        """

        # 숫자나 다른 타입이 전달되어도 출력할 수 있도록
        # 문자열로 변환한다.
        text = str(message)

        # 이전 메시지와 현재 메시지 중 더 긴 길이를 사용한다.
        #
        # 현재 메시지가 더 짧더라도 이전 메시지 길이만큼
        # 오른쪽에 공백을 채워 이전 글자가 남지 않게 한다.
        width = max(
            self.last_width,
            len(text),
        )

        # "\r":
        #   줄바꿈하지 않고 현재 줄의 맨 앞으로 커서를 이동한다.
        #
        # text.ljust(width):
        #   문자열 오른쪽에 공백을 채워 전체 길이를 width로 맞춘다.
        #
        # 따라서 이전 메시지를 현재 메시지와 공백으로 덮어쓴다.
        sys.stdout.write(
            "\r" + text.ljust(width)
        )

        # 출력 버퍼에 기다리지 않고
        # 현재 상태 메시지를 즉시 콘솔에 표시한다.
        sys.stdout.flush()

        # 현재 한 줄 상태 메시지가 출력 중임을 표시한다.
        self.active = True

        # 현재 출력 너비를 저장한다.
        #
        # 다음 update() 호출 시 이전 글자를 완전히 지우는 데 사용한다.
        self.last_width = width

    def finish(self) -> None:
        """
        한 줄 덮어쓰기 상태 출력을 종료하고 다음 줄로 이동한다.

        이후 print()나 logger가 상태 메시지 뒤에 붙지 않고
        새로운 줄에서 정상적으로 출력되게 한다.
        """

        # update()가 호출되지 않았거나 이미 finish()가 호출된 경우
        # 종료할 상태 출력이 없으므로 아무 작업도 하지 않는다.
        if not self.active:
            return

        # 현재 한 줄 상태 출력을 끝내고 다음 줄로 이동한다.
        sys.stdout.write("\n")

        # 줄바꿈을 즉시 콘솔에 반영한다.
        sys.stdout.flush()

        # 한 줄 상태 출력이 종료되었음을 표시한다.
        self.active = False

        # 이전 메시지 너비를 초기화한다.
        #
        # 다음에 update()를 다시 사용할 때
        # 새로운 상태 출력으로 시작할 수 있다.
        self.last_width = 0


def cleanup_old_logs(
        log_dir: Path,
        retention_days: int,
) -> None:
    """보관기간이 지난 일별 로그 파일을 삭제한다."""
    if retention_days <= 0:
        return

    cutoff_date = datetime.now().date() - timedelta(
        days=retention_days
    )

    for path in log_dir.glob(
            "crawl-daemon-*.log"
    ):
        try:
            log_date = datetime.strptime(
                path.stem.removeprefix("crawl-daemon-"),
                "%Y-%m-%d",
            ).date()

            if log_date < cutoff_date:
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