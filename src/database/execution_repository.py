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
from typing import Any, Dict, Optional, Set, Union

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
class PandaExecutionSetting:
    """SETTING_JSON에서 읽은 팬더티비 작업 설정."""

    ranking_type: str
    start_page: int
    end_page: int
    message_text: str
    exclude_duplicate_yn: bool
    login_id: str
    login_password: str


@dataclass(frozen=True)
class NaverShopExecutionSetting:
    """SETTING_JSON에서 읽은 네이버 쇼핑 수집 설정."""

    keyword: str
    start_page: int
    end_page: int
    site_total_cnt: int
    cpcha_cnt: int
    dup_yn: bool


ExecutionSetting = Union[
    PandaExecutionSetting,
    NaverShopExecutionSetting,
]


@dataclass(frozen=True)
class ExecutionJob:
    """RUNNING으로 선점한 실행 작업."""

    hist_id: int
    user_service_id: int
    status: str
    scheduled_at: str
    setting_json: str
    setting: ExecutionSetting
    service_code: str


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



def parse_setting_json(setting_json: str) -> Dict[str, Any]:
    """SETTING_JSON을 dict로 읽고 fields 형식까지 정규화한다."""
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

    return normalize_setting_data(loaded_data)


def parse_panda_execution_setting(
        data: Dict[str, Any],
) -> PandaExecutionSetting:
    """팬더 메시지 작업 설정을 검증한다."""

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

    return PandaExecutionSetting(
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


def parse_naver_shop_execution_setting(
        data: Dict[str, Any],
) -> NaverShopExecutionSetting:
    """네이버 쇼핑 수집 작업 설정을 검증한다."""
    keyword = str(data.get("keyword") or "").strip()

    try:
        start_page = int(data.get("start_page", 1))
        end_page = int(data.get("end_page", start_page))
        site_total_cnt = int(data.get("site_total_cnt", 0))
        cpcha_cnt = int(data.get("cpcha_cnt", 5))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "start_page, end_page, site_total_cnt, cpcha_cnt는 숫자여야 합니다."
        ) from error

    if not keyword:
        raise ValueError("keyword 값이 없습니다.")
    if start_page < 1:
        raise ValueError("start_page는 1 이상이어야 합니다.")
    if end_page < start_page:
        raise ValueError("end_page는 start_page보다 크거나 같아야 합니다.")
    if site_total_cnt < 0:
        raise ValueError("site_total_cnt는 0 이상이어야 합니다.")
    if cpcha_cnt < 1:
        raise ValueError("cpcha_cnt는 1 이상이어야 합니다.")

    return NaverShopExecutionSetting(
        keyword=keyword,
        start_page=start_page,
        end_page=end_page,
        site_total_cnt=site_total_cnt,
        cpcha_cnt=cpcha_cnt,
        dup_yn=normalize_boolean(data.get("dup_yn")),
    )


def parse_execution_setting(
        service_code: str,
        setting_json: str,
) -> ExecutionSetting:
    """서비스 코드에 맞는 SETTING_JSON을 파싱하고 검증한다."""
    data = parse_setting_json(setting_json)

    if service_code == "PANDA_MESSAGE":
        return parse_panda_execution_setting(data)

    if service_code == "NAVER_SHOP_TOTAL_DETAIL":
        return parse_naver_shop_execution_setting(data)

    raise ValueError(f"지원하지 않는 SERVICE_CODE입니다: {service_code}")


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


def select_existing_store_names(
        connection: Connection,
        user_service_id: int,
) -> Set[str]:
    """
    같은 USER_SERVICE_ID의 과거 SUCCESS 결과를 조회하고,
    RESULT_JSON의 store_name을 Python에서 추출한다.

    구버전 MariaDB 호환을 위해 JSON_EXTRACT,
    JSON_UNQUOTE 등의 DB JSON 함수는 사용하지 않는다.
    """
    sql = """
          SELECT
              R.RESULT_JSON
          FROM SERVICE_EXECUTION_RESULT R
                   INNER JOIN SERVICE_EXECUTION_HIST H
                              ON H.HIST_ID = R.HIST_ID
          WHERE H.USER_SERVICE_ID = %s
            AND R.STATUS = 'SUCCESS'
            AND R.RESULT_JSON IS NOT NULL
            AND TRIM(R.RESULT_JSON) <> '' \
          """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (user_service_id,),
        )
        rows = cursor.fetchall()

    store_names: Set[str] = set()

    for row in rows:
        result_json = row.get("RESULT_JSON")

        if not result_json:
            continue

        try:
            result_data = json.loads(result_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        if not isinstance(result_data, dict):
            continue

        store_name = str(
            result_data.get("store_name") or ""
        ).strip()

        if store_name:
            store_names.add(store_name)

    return store_names



def claim_next_ready_execution(
        connection: Connection,
        service_code: str,
) -> Optional[ExecutionJob]:
    """담당 서비스의 가장 이른 READY 실행 이력 1건을 RUNNING으로 선점한다."""
    current_time = current_datetime_text()

    select_sql = """
        SELECT
            H.HIST_ID,
            H.USER_SERVICE_ID,
            H.STATUS,
            H.SCHEDULED_AT,
            H.SETTING_JSON,
            S.SERVICE_CODE
        FROM SERVICE_EXECUTION_HIST H
        INNER JOIN USER_SERVICE U
            ON U.USER_SERVICE_ID = H.USER_SERVICE_ID
        INNER JOIN SERVICE_MASTER S
            ON S.SERVICE_ID = U.SERVICE_ID
        WHERE H.STATUS = 'READY'
          AND H.SCHEDULED_AT <= %s
          AND S.SERVICE_CODE = %s
        ORDER BY
            H.SCHEDULED_AT ASC,
            H.HIST_ID ASC
        LIMIT 1
    """

    with connection.cursor() as cursor:
        cursor.execute(
            select_sql,
            (current_time, service_code),
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
            service_code=service_code,
            setting_json=setting_json,
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
        service_code=service_code,
    )
