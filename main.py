# -*- coding: utf-8 -*-
"""
crawl-deamon 메인 실행 파일

실행 흐름
1. MariaDB 연결 후 READY 작업 대기
2. READY 작업을 RUNNING으로 선점
3. 작업마다 새 Chrome 브라우저 실행
4. SETTING_JSON의 아이디/비밀번호로 팬더라이브 자동 로그인
5. 랭킹 조회 및 상세 결과 선INSERT
6. 대상별 실제 메시지 발송
7. 실행 이력을 SUCCESS / FAIL로 변경
8. 작업 완료 여부와 관계없이 브라우저 종료
9. 데몬은 브라우저 없이 다음 READY 작업 대기
"""

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pymysql
from pymysql.connections import Connection

from src.database.db_connection import create_connection
from src.database.execution_repository import (
    ExecutionJob,
    claim_next_ready_execution,
    insert_execution_result,
    mark_execution_fail,
    mark_execution_success,
    update_execution_result,
)
from src.utils.daemon_logger import (
    ConsoleStatus,
    cleanup_old_logs,
    setup_logger,
)
from src.utils.selenium_utils import SeleniumUtils
from src.workers.panda_worker import (
    MessageExecutionResult,
    execute_message_job,
)


PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"

JOB_POLL_INTERVAL_SEC = 5
DB_RECONNECT_INTERVAL_SEC = 10
FILE_HEARTBEAT_INTERVAL_SEC = 300
LOG_RETENTION_DAYS = 30


def check_database_connection(connection: Connection) -> None:
    """DB 연결을 처음 생성했을 때 정상 여부를 확인한다."""
    connection.ping()

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 AS result")
        row = cursor.fetchone()

    if not row or row.get("result") != 1:
        raise RuntimeError(
            f"DB 연결 확인 결과가 올바르지 않습니다: {row}"
        )


def close_database_connection(
        connection: Optional[Connection],
) -> None:
    """DB 연결을 안전하게 종료한다."""
    if not connection:
        return

    try:
        connection.close()
    except Exception:
        pass


def connect_database_until_success(
        logger,
        console_status: ConsoleStatus,
) -> Connection:
    """MariaDB에 연결될 때까지 새 연결 생성을 반복한다."""
    while True:
        connection: Optional[Connection] = None

        try:
            console_status.finish()
            logger.info("[DB] MariaDB에 연결합니다.")

            connection = create_connection()
            check_database_connection(connection)

            logger.info("[DB] MariaDB 연결 성공")
            return connection

        except Exception as error:
            close_database_connection(connection)

            logger.error(
                "[DB] MariaDB 연결 실패: %s",
                error,
            )
            logger.info(
                "[DB] %s초 후 새 연결을 생성합니다.",
                DB_RECONNECT_INTERVAL_SEC,
            )

            time.sleep(DB_RECONNECT_INTERVAL_SEC)


def ensure_database_connection(
        connection: Optional[Connection],
        logger,
        console_status: ConsoleStatus,
) -> Connection:
    """기존 DB 연결을 검사하고 끊겼다면 새 연결을 반환한다."""
    if connection:
        try:
            connection.ping()
            return connection

        except Exception as error:
            console_status.finish()
            logger.warning(
                "[DB] 기존 연결이 끊겼습니다: %s",
                error,
            )

    close_database_connection(connection)

    return connect_database_until_success(
        logger=logger,
        console_status=console_status,
    )



def cleanup_logs_if_due(
        next_cleanup_at: datetime,
        logger,
        console_status: ConsoleStatus,
) -> datetime:
    """하루에 한 번 오래된 로그 파일을 삭제한다."""
    now = datetime.now()

    if now < next_cleanup_at:
        return next_cleanup_at

    console_status.finish()

    cleanup_old_logs(
        log_dir=LOG_DIR,
        retention_days=LOG_RETENTION_DAYS,
    )

    logger.info(
        "[LOG] %s일이 지난 로그 파일 정리를 완료했습니다.",
        LOG_RETENTION_DAYS,
    )

    return (
            now + timedelta(days=1)
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def print_claimed_job(
        job: ExecutionJob,
        logger,
        console_status: ConsoleStatus,
) -> None:
    """RUNNING으로 선점한 작업 설정을 출력한다."""
    console_status.finish()

    logger.info("=" * 70)
    logger.info("[QUEUE] 실행 작업을 RUNNING으로 선점했습니다.")
    logger.info("HIST_ID=%s", job.hist_id)
    logger.info("USER_SERVICE_ID=%s", job.user_service_id)
    logger.info("SCHEDULED_AT=%s", job.scheduled_at)
    logger.info("ranking_type=%s", job.setting.ranking_type)
    logger.info(
        "page_range=%s~%s",
        job.setting.start_page,
        job.setting.end_page,
    )
    logger.info(
        "exclude_duplicate_yn=%s",
        job.setting.exclude_duplicate_yn,
    )
    logger.info("login_id=%s", job.setting.login_id)
    logger.info("=" * 70)


def insert_result_with_reconnect(
        connection: Optional[Connection],
        hist_id: int,
        status: str,
        result_data: Dict[str, Any],
        logger,
        console_status: ConsoleStatus,
        error_message: Optional[str] = None,
) -> tuple[Connection, int]:
    """상세 결과 INSERT가 완료될 때까지 DB 연결을 복구하며 재시도한다."""
    while True:
        connection = ensure_database_connection(
            connection=connection,
            logger=logger,
            console_status=console_status,
        )

        try:
            result_id = insert_execution_result(
                connection=connection,
                hist_id=hist_id,
                status=status,
                result_data=result_data,
                error_message=error_message,
            )

            return connection, result_id

        except pymysql.MySQLError as error:
            console_status.finish()
            logger.error(
                "[DB] 상세 결과 INSERT 실패 "
                "| HIST_ID=%s | STATUS=%s | ERROR=%s",
                hist_id,
                status,
                error,
            )

            close_database_connection(connection)
            connection = None

            time.sleep(DB_RECONNECT_INTERVAL_SEC)


def update_result_with_reconnect(
        connection: Optional[Connection],
        result_id: int,
        status: str,
        result_data: Dict[str, Any],
        logger,
        console_status: ConsoleStatus,
        error_message: Optional[str] = None,
) -> Connection:
    """상세 결과 UPDATE가 완료될 때까지 DB 연결을 복구하며 재시도한다."""
    while True:
        connection = ensure_database_connection(
            connection=connection,
            logger=logger,
            console_status=console_status,
        )

        try:
            updated = update_execution_result(
                connection=connection,
                result_id=result_id,
                status=status,
                result_data=result_data,
                error_message=error_message,
            )

            if not updated:
                raise RuntimeError(
                    f"RESULT_ID={result_id} 상세 결과 변경 대상이 없습니다."
                )

            return connection

        except pymysql.MySQLError as error:
            console_status.finish()
            logger.error(
                "[DB] 상세 결과 UPDATE 실패 "
                "| RESULT_ID=%s | STATUS=%s | ERROR=%s",
                result_id,
                status,
                error,
            )

            close_database_connection(connection)
            connection = None

            time.sleep(DB_RECONNECT_INTERVAL_SEC)


class ExecutionResultStore:
    """Worker에서 상세 결과 INSERT와 UPDATE를 호출할 때 사용하는 저장 객체."""

    def __init__(
            self,
            connection: Optional[Connection],
            hist_id: int,
            logger,
            console_status: ConsoleStatus,
    ) -> None:
        self.connection = connection
        self.hist_id = hist_id
        self.logger = logger
        self.console_status = console_status

        self.inserted_count = 0
        self.updated_success_count = 0
        self.updated_fail_count = 0
        self.skip_count = 0

    def insert(
            self,
            status: str,
            result_data: Dict[str, Any],
            error_message: Optional[str] = None,
    ) -> int:
        """SERVICE_EXECUTION_RESULT에 한 건 INSERT하고 RESULT_ID를 반환한다."""
        self.connection, result_id = insert_result_with_reconnect(
            connection=self.connection,
            hist_id=self.hist_id,
            status=status,
            result_data=result_data,
            logger=self.logger,
            console_status=self.console_status,
            error_message=error_message,
        )

        self.inserted_count += 1

        if status.upper() == "SKIP":
            self.skip_count += 1

        return result_id

    def update(
            self,
            result_id: int,
            status: str,
            result_data: Dict[str, Any],
            error_message: Optional[str] = None,
    ) -> None:
        """이미 INSERT된 상세 결과의 상태와 JSON을 변경한다."""
        self.connection = update_result_with_reconnect(
            connection=self.connection,
            result_id=result_id,
            status=status,
            result_data=result_data,
            logger=self.logger,
            console_status=self.console_status,
            error_message=error_message,
        )

        if status.upper() == "SUCCESS":
            self.updated_success_count += 1
        elif status.upper() == "FAIL":
            self.updated_fail_count += 1


def mark_status_with_reconnect(
        connection: Optional[Connection],
        updater: Callable[..., bool],
        hist_id: int,
        logger,
        console_status: ConsoleStatus,
        error_message: Optional[str] = None,
) -> Connection:
    """실행 이력 상태 변경이 완료될 때까지 DB 연결을 복구한다."""
    while True:
        connection = ensure_database_connection(
            connection=connection,
            logger=logger,
            console_status=console_status,
        )

        try:
            if error_message is None:
                updated = updater(
                    connection=connection,
                    hist_id=hist_id,
                )
            else:
                updated = updater(
                    connection=connection,
                    hist_id=hist_id,
                    error_message=error_message,
                )

            if not updated:
                raise RuntimeError(
                    f"HIST_ID={hist_id} 상태 변경 대상이 없습니다."
                )

            return connection

        except pymysql.MySQLError as error:
            console_status.finish()
            logger.error(
                "[DB] HIST_ID=%s 상태 업데이트 실패: %s",
                hist_id,
                error,
            )

            close_database_connection(connection)
            connection = None

            time.sleep(DB_RECONNECT_INTERVAL_SEC)


def build_job_error_message(
        result: MessageExecutionResult,
) -> str:
    """실행 이력 ERROR_MESSAGE에 저장할 전체 결과 요약을 만든다."""
    summary = (
        f"메시지 발송 결과: "
        f"SUCCESS={result.success_count}, "
        f"FAIL={result.fail_count}, "
        f"SKIP={result.skipped_count}"
    )

    if result.stop_reason:
        return (
            f"{summary}, 중단사유={result.stop_reason}"
        )

    return summary


def log_job_result(
        job: ExecutionJob,
        result: MessageExecutionResult,
        final_status: str,
        logger,
) -> None:
    """실제 메시지 발송 작업의 최종 결과를 출력한다."""
    logger.info(
        "[JOB] %s | HIST_ID=%s | 조회=%s | 최종대상=%s | "
        "선INSERT=%s | SUCCESS=%s | FAIL=%s | SKIP=%s",
        final_status,
        job.hist_id,
        result.fetched_count,
        result.target_count,
        result.inserted_count,
        result.success_count,
        result.fail_count,
        result.skipped_count,
    )


def main() -> None:
    """브라우저 없이 대기하고, 작업이 있을 때만 Chrome을 실행한다."""
    logger = setup_logger(
        project_root=PROJECT_ROOT,
        retention_days=LOG_RETENTION_DAYS,
    )
    console_status = ConsoleStatus()

    connection: Optional[Connection] = None

    try:
        logger.info("=" * 70)
        logger.info("[DAEMON] crawl-deamon을 시작합니다.")
        logger.info("=" * 70)

        connection = connect_database_until_success(
            logger=logger,
            console_status=console_status,
        )

        logger.info(
            "[QUEUE] %s초마다 READY 작업을 확인합니다.",
            JOB_POLL_INTERVAL_SEC,
        )
        logger.info(
            "[BROWSER] READY 작업이 있을 때만 Chrome을 실행합니다."
        )
        logger.info(
            "[BROWSER] 작업이 끝나면 Chrome을 종료하고 데몬만 대기합니다."
        )
        logger.info("[DAEMON] 종료하려면 Ctrl+C를 누르세요.")

        next_file_heartbeat_at = (
                time.monotonic() + FILE_HEARTBEAT_INTERVAL_SEC
        )
        # 현재 시간 가져와서 하루 더하고 자정으로 변경
        next_log_cleanup_at = (
                datetime.now() + timedelta(days=1)
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        while True:
            connection = ensure_database_connection(
                connection=connection,
                logger=logger,
                console_status=console_status,
            )

            try:
                job = claim_next_ready_execution(connection)

            except ValueError as error:
                console_status.finish()
                logger.error(
                    "[QUEUE] 잘못된 설정의 작업을 FAIL 처리했습니다: %s",
                    error,
                )
                continue

            except pymysql.MySQLError as error:
                console_status.finish()
                logger.error(
                    "[DB] 작업 조회 중 연결 오류: %s",
                    error,
                )

                close_database_connection(connection)
                connection = None

                time.sleep(DB_RECONNECT_INTERVAL_SEC)
                continue

            if job is None:
                now_text = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                console_status.update(
                    f"[{now_text}] [QUEUE] READY 작업 없음 "
                    f"| {JOB_POLL_INTERVAL_SEC}초 후 재조회"
                )

                now_monotonic = time.monotonic()

                if now_monotonic >= next_file_heartbeat_at:
                    console_status.finish()
                    logger.info(
                        "[HEARTBEAT] 데몬 정상 작동 중 | READY 작업 없음"
                    )
                    next_file_heartbeat_at = (
                            now_monotonic + FILE_HEARTBEAT_INTERVAL_SEC
                    )

                next_log_cleanup_at = cleanup_logs_if_due(
                    next_cleanup_at=next_log_cleanup_at,
                    logger=logger,
                    console_status=console_status,
                )

                time.sleep(JOB_POLL_INTERVAL_SEC)
                continue

            print_claimed_job(
                job=job,
                logger=logger,
                console_status=console_status,
            )

            result_store = ExecutionResultStore(
                connection=connection,
                hist_id=job.hist_id,
                logger=logger,
                console_status=console_status,
            )

            # 변경: 브라우저와 SeleniumUtils를 작업마다 새로 생성한다.
            selenium_utils = SeleniumUtils(
                headless=False,
                debug=True,
                log_func=lambda message: logger.info(message),
            )

            try:
                logger.info(
                    "[SELENIUM] HIST_ID=%s | Chrome 브라우저를 실행합니다.",
                    job.hist_id,
                )

                driver = selenium_utils.start_driver(
                    timeout=30,
                    view_mode="browser",
                    window_size=(1200, 900),
                )

                logger.info(
                    "[SELENIUM] HIST_ID=%s | Chrome 브라우저 실행 성공",
                    job.hist_id,
                )

                # execute_message_job 내부에서 자동 로그인 후 기존 작업을 진행한다.
                result = execute_message_job(
                    driver=driver,
                    selenium_utils=selenium_utils,
                    job=job,
                    logger=logger,
                    result_inserter=result_store.insert,
                    result_updater=result_store.update,
                )

                connection = result_store.connection

                # 변경: 일부 대상 실패가 있어도 작업이 끝까지 완료되면 전체 SUCCESS로 처리한다.
                # 단, 발송 제한 등으로 중간에 멈춘 경우는 전체 FAIL로 처리한다.
                if not result.stopped_early:
                    final_status = "SUCCESS"

                    connection = mark_status_with_reconnect(
                        connection=connection,
                        updater=mark_execution_success,
                        hist_id=job.hist_id,
                        logger=logger,
                        console_status=console_status,
                    )

                else:
                    final_status = "FAIL"

                    connection = mark_status_with_reconnect(
                        connection=connection,
                        updater=mark_execution_fail,
                        hist_id=job.hist_id,
                        logger=logger,
                        console_status=console_status,
                        error_message=build_job_error_message(result),
                    )

                log_job_result(
                    job=job,
                    result=result,
                    final_status=final_status,
                    logger=logger,
                )

            except KeyboardInterrupt:
                console_status.finish()
                connection = result_store.connection

                try:
                    connection = mark_status_with_reconnect(
                        connection=connection,
                        updater=mark_execution_fail,
                        hist_id=job.hist_id,
                        logger=logger,
                        console_status=console_status,
                        error_message="사용자가 Ctrl+C로 작업을 중단했습니다.",
                    )
                except Exception:
                    logger.exception(
                        "[JOB] HIST_ID=%s 사용자 중단 상태 저장 실패",
                        job.hist_id,
                    )

                raise

            except Exception as error:
                console_status.finish()

                final_status = "FAIL"

                logger.exception(
                    "[JOB] %s | HIST_ID=%s | INSERT=%s | "
                    "SUCCESS=%s | ERROR=%s",
                    final_status,
                    job.hist_id,
                    result_store.inserted_count,
                    result_store.updated_success_count,
                    error,
                )

                connection = result_store.connection

                connection = mark_status_with_reconnect(
                    connection=connection,
                    updater=mark_execution_fail,
                    hist_id=job.hist_id,
                    logger=logger,
                    console_status=console_status,
                    error_message=str(error),
                )

            finally:
                # 변경: 성공/실패와 관계없이 작업이 끝나면 브라우저를 종료한다.
                selenium_utils.quit()
                logger.info(
                    "[SELENIUM] HIST_ID=%s | Chrome 브라우저를 종료했습니다.",
                    job.hist_id,
                )


                next_log_cleanup_at = cleanup_logs_if_due(
                    next_cleanup_at=next_log_cleanup_at,
                    logger=logger,
                    console_status=console_status,
                )

    except KeyboardInterrupt:
        console_status.finish()
        logger.info("[DAEMON] 사용자가 Ctrl+C로 실행을 중단했습니다.")

    except Exception as error:
        console_status.finish()
        logger.exception(
            "[DAEMON] 실행 중 치명적인 오류가 발생했습니다: %s",
            error,
        )
        raise

    finally:
        console_status.finish()

        close_database_connection(connection)
        logger.info("[DB] MariaDB 연결을 종료했습니다.")

        logger.info("[DAEMON] crawl-deamon을 종료했습니다.")


if __name__ == "__main__":
    main()