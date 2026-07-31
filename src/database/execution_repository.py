# -*- coding: utf-8 -*-
"""
SERVICE_EXECUTION_HIST 및 SERVICE_EXECUTION_RESULT DB 처리

실행 이력 상태
- READY -> RUNNING
- RUNNING -> SUCCESS / PARTIAL_FAIL / FAIL

상세 결과 상태
- READY   : DB INSERT 완료, 발송 대기
- SUCCESS : 발송 성공
- FAIL    : 발송 실패
- SKIP    : 발송 제외
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from pymysql.connections import Connection


ALLOWED_RANKING_TYPES = {
    "rankingPersonalBJ",
    "rankingNewBJ",
    "rankingCrewBJ",
    "rankingPopular",
}

ALLOWED_RESULT_STATUSES = {
    "READY",
    "SUCCESS",
    "FAIL",
    "SKIP",
}


@dataclass(frozen=True)
class ExecutionSetting:
    """SETTING_JSON에서 읽은 팬더티비 작업 설정."""

    ranking_type: str
    start_page: int
    end_page: int
    message_text: str
    exclude_duplicate_yn: bool
    login_id: str
    login_password: str


@dataclass(frozen=True)
class ExecutionJob:
    """RUNNING으로 선점한 실행 작업."""

    hist_id: int
    user_service_id: int
    status: str
    scheduled_at: str
    setting_json: str
    setting: ExecutionSetting


def current_datetime_text() -> str:
    """yyyy-MM-dd HH:mm:ss.SSS 형식으로 현재 시각을 반환한다."""
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]


def normalize_boolean(value: Any) -> bool:
    """JSON 값을 bool로 변환한다."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value == 1

    if isinstance(value, str):
        return value.strip().lower() in {
            "true",
            "1",
            "y",
            "yes",
        }

    return False


def normalize_setting_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    일반 설정 JSON과 fields 배열 형태를 모두 동일한 dict로 변환한다.

    지원 형식
    1. {"ranking_type": "...", "login_id": "..."}
    2. {"version": 1, "fields": [{"code": "ranking_type", "value": "..."}]}
    """
    normalized = dict(data)
    fields = data.get("fields")

    if not isinstance(fields, list):
        return normalized

    for field in fields:
        if not isinstance(field, dict):
            continue

        code = str(field.get("code") or "").strip()
        if not code:
            continue

        normalized[code] = field.get("value")

    return normalized



def parse_execution_setting(
        setting_json: str,
) -> ExecutionSetting:
    """SETTING_JSON을 파싱하고 필수 설정을 검증한다."""
    try:
        loaded_data = json.loads(setting_json)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"SETTING_JSON 형식이 올바르지 않습니다: {error}"
        ) from error

    if not isinstance(loaded_data, dict):
        raise ValueError(
            "SETTING_JSON의 최상위 값은 객체여야 합니다."
        )

    # 변경: 사용자가 전달한 version/fields 형식도 바로 읽을 수 있다.
    data = normalize_setting_data(loaded_data)

    ranking_type = str(
        data.get("ranking_type", "")
    ).strip()

    message_text = str(
        data.get("message_text", "")
    ).strip()

    login_id = str(data.get("login_id") or "").strip()
    login_password = str(data.get("login_password") or "")

    try:
        start_page = int(data.get("start_page"))
        end_page = int(data.get("end_page"))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "start_page와 end_page는 숫자여야 합니다."
        ) from error

    if ranking_type not in ALLOWED_RANKING_TYPES:
        raise ValueError(
            f"지원하지 않는 ranking_type입니다: {ranking_type}"
        )

    if start_page < 1:
        raise ValueError(
            "start_page는 1 이상이어야 합니다."
        )

    if end_page < start_page:
        raise ValueError(
            "end_page는 start_page보다 크거나 같아야 합니다."
        )

    if not message_text:
        raise ValueError(
            "message_text 값이 없습니다."
        )

    if not login_id:
        raise ValueError(
            "login_id 값이 없습니다."
        )

    if not login_password:
        raise ValueError(
            "login_password 값이 없습니다."
        )

    return ExecutionSetting(
        ranking_type=ranking_type,
        start_page=start_page,
        end_page=end_page,
        message_text=message_text,
        exclude_duplicate_yn=normalize_boolean(
            data.get("exclude_duplicate_yn")
        ),
        login_id=login_id,
        login_password=login_password,
    )


def truncate_error_message(
        error_message: Optional[str],
) -> Optional[str]:
    """ERROR_MESSAGE varchar(5000)에 맞게 오류 내용을 자른다."""
    if not error_message:
        return None

    return str(error_message)[:5000]


def json_text(result_data: Dict[str, Any]) -> str:
    """RESULT_JSON에 저장할 JSON 문자열을 생성한다."""
    if not isinstance(result_data, dict):
        raise ValueError(
            "result_data는 dict 형식이어야 합니다."
        )

    return json.dumps(
        result_data,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def validate_result_status(status: str) -> str:
    """상세 결과 상태를 검증하고 대문자로 반환한다."""
    normalized_status = str(
        status or ""
    ).strip().upper()

    if normalized_status not in ALLOWED_RESULT_STATUSES:
        raise ValueError(
            f"지원하지 않는 상세 결과 STATUS입니다: {status}"
        )

    return normalized_status


def insert_execution_result(
        connection: Connection,
        hist_id: int,
        status: str,
        result_data: Dict[str, Any],
        error_message: Optional[str] = None,
) -> int:
    """SERVICE_EXECUTION_RESULT에 상세 결과 한 건을 INSERT한다."""
    sql = """
        INSERT INTO SERVICE_EXECUTION_RESULT
        (
            HIST_ID,
            STATUS,
            RESULT_JSON,
            ERROR_MESSAGE,
            PROCESSED_AT
        )
        VALUES
        (
            %s,
            %s,
            %s,
            %s,
            %s
        )
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    hist_id,
                    validate_result_status(status),
                    json_text(result_data),
                    truncate_error_message(error_message),
                    current_datetime_text(),
                ),
            )
            result_id = int(cursor.lastrowid)

        connection.commit()
        return result_id

    except Exception:
        connection.rollback()
        raise


def update_execution_result(
        connection: Connection,
        result_id: int,
        status: str,
        result_data: Dict[str, Any],
        error_message: Optional[str] = None,
) -> bool:
    """먼저 INSERT된 READY 상세 결과를 SUCCESS / FAIL 등으로 변경한다."""
    sql = """
        UPDATE SERVICE_EXECUTION_RESULT
        SET STATUS = %s,
            RESULT_JSON = %s,
            ERROR_MESSAGE = %s,
            PROCESSED_AT = %s
        WHERE RESULT_ID = %s
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    validate_result_status(status),
                    json_text(result_data),
                    truncate_error_message(error_message),
                    current_datetime_text(),
                    result_id,
                ),
            )
            updated = cursor.rowcount == 1

        connection.commit()
        return updated

    except Exception:
        connection.rollback()
        raise


def mark_execution_success(
        connection: Connection,
        hist_id: int,
) -> bool:
    """RUNNING 실행 이력을 SUCCESS로 변경한다."""
    sql = """
        UPDATE SERVICE_EXECUTION_HIST
        SET STATUS = 'SUCCESS',
            END_AT = %s,
            ERROR_MESSAGE = NULL
        WHERE HIST_ID = %s
          AND STATUS = 'RUNNING'
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    current_datetime_text(),
                    hist_id,
                ),
            )
            updated = cursor.rowcount == 1

        connection.commit()
        return updated

    except Exception:
        connection.rollback()
        raise


def mark_execution_partial_fail(
        connection: Connection,
        hist_id: int,
        error_message: str,
) -> bool:
    """RUNNING 실행 이력을 PARTIAL_FAIL로 변경한다."""
    sql = """
        UPDATE SERVICE_EXECUTION_HIST
        SET STATUS = 'PARTIAL_FAIL',
            END_AT = %s,
            ERROR_MESSAGE = %s
        WHERE HIST_ID = %s
          AND STATUS = 'RUNNING'
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    current_datetime_text(),
                    truncate_error_message(error_message),
                    hist_id,
                ),
            )
            updated = cursor.rowcount == 1

        connection.commit()
        return updated

    except Exception:
        connection.rollback()
        raise


def mark_execution_fail(
        connection: Connection,
        hist_id: int,
        error_message: str,
) -> bool:
    """RUNNING 실행 이력을 FAIL로 변경한다."""
    sql = """
        UPDATE SERVICE_EXECUTION_HIST
        SET STATUS = 'FAIL',
            END_AT = %s,
            ERROR_MESSAGE = %s
        WHERE HIST_ID = %s
          AND STATUS = 'RUNNING'
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    current_datetime_text(),
                    truncate_error_message(error_message),
                    hist_id,
                ),
            )
            updated = cursor.rowcount == 1

        connection.commit()
        return updated

    except Exception:
        connection.rollback()
        raise


def claim_next_ready_execution(
        connection: Connection,
) -> Optional[ExecutionJob]:
    """예약시간이 지난 가장 이른 READY 실행 이력 1건을 RUNNING으로 선점한다."""
    current_time = current_datetime_text()

    select_sql = """
        SELECT
            HIST_ID,
            USER_SERVICE_ID,
            STATUS,
            SCHEDULED_AT,
            SETTING_JSON
        FROM SERVICE_EXECUTION_HIST
        WHERE STATUS = 'READY'
          AND SCHEDULED_AT <= %s
        ORDER BY
            SCHEDULED_AT ASC,
            HIST_ID ASC
        LIMIT 1
    """

    with connection.cursor() as cursor:
        cursor.execute(
            select_sql,
            (current_time,),
        )
        row: Optional[Dict[str, Any]] = cursor.fetchone()

    if not row:
        return None

    hist_id = int(row["HIST_ID"])

    update_sql = """
        UPDATE SERVICE_EXECUTION_HIST
        SET STATUS = 'RUNNING',
            START_AT = %s,
            END_AT = NULL,
            ERROR_MESSAGE = NULL
        WHERE HIST_ID = %s
          AND STATUS = 'READY'
          AND SCHEDULED_AT <= %s
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                update_sql,
                (
                    current_time,
                    hist_id,
                    current_time,
                ),
            )
            claimed = cursor.rowcount == 1

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    if not claimed:
        return None

    setting_json = str(
        row.get("SETTING_JSON") or ""
    )

    try:
        setting = parse_execution_setting(
            setting_json
        )

    except Exception as error:
        mark_execution_fail(
            connection=connection,
            hist_id=hist_id,
            error_message=str(error),
        )
        raise

    return ExecutionJob(
        hist_id=hist_id,
        user_service_id=int(
            row["USER_SERVICE_ID"]
        ),
        status="RUNNING",
        scheduled_at=str(
            row["SCHEDULED_AT"]
        ),
        setting_json=setting_json,
        setting=setting,
    )
