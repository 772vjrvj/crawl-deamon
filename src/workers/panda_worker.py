# -*- coding: utf-8 -*-
"""
팬더티비 랭킹 조회 및 실제 메시지 발송 Worker

핵심 순서
1. 랭킹 대상 전체 조회
2. 모든 대상을 SERVICE_EXECUTION_RESULT에 먼저 INSERT
   - 발송 가능 대상: READY
   - userIdx 없음: SKIP
3. INSERT 단계가 전부 끝난 뒤 실제 메시지 발송 시작
4. 대상별 응답에 따라 READY를 SUCCESS 또는 FAIL로 변경
5. 발송 제한이 발생하면 남은 READY 대상을 SKIP으로 변경하고 중단
"""

import json
import random
import string
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from src.database.execution_repository import ExecutionJob
from src.utils.selenium_utils import SeleniumUtils


RANKING_API_URL = "https://api.pandalive.co.kr/v1/live/cache"
SEND_MESSAGE_API_URL = "https://api.pandalive.co.kr/v1/post/send_message"

LOGIN_BUTTON_TEXT = "로그인 / 회원가입"
MESSAGE_LIMIT_TEXT = "쪽지 전송이 제한되었습니다"

PAGE_SIZE = 20

# 실제 쪽지 발송 사이의 랜덤 대기 시간
MESSAGE_SEND_DELAY_MIN_SEC = 5
MESSAGE_SEND_DELAY_MAX_SEC = 7

RANKING_PAGE_URLS = {
    "rankingPersonalBJ": (
        "https://www.pandalive.co.kr/ranking/rankingPersonalBJ"
    ),
    "rankingNewBJ": (
        "https://www.pandalive.co.kr/ranking/rankingNewBJ"
    ),
    "rankingCrewBJ": (
        "https://www.pandalive.co.kr/ranking/rankingCrewBJ"
    ),
    "rankingPopular": (
        "https://www.pandalive.co.kr/ranking/rankingPopular"
    ),
}


@dataclass(frozen=True)
class PreparedResult:
    """먼저 INSERT된 발송 대기 상세 결과."""

    result_id: int
    result_data: Dict[str, Any]


@dataclass(frozen=True)
class MessageExecutionResult:
    """실제 메시지 발송 전체 결과."""

    fetched_count: int
    target_count: int
    inserted_count: int
    success_count: int
    fail_count: int
    skipped_count: int
    stopped_early: bool
    stop_reason: Optional[str]


ResultInserter = Callable[
    [str, Dict[str, Any], Optional[str]],
    int,
]

ResultUpdater = Callable[
    [int, str, Dict[str, Any], Optional[str]],
    None,
]


def is_panda_logged_out(driver: WebDriver) -> bool:
    """'로그인 / 회원가입' 문구가 보이면 로그아웃으로 판단한다."""
    login_buttons = driver.find_elements(
        By.XPATH,
        (
            "//button["
            "normalize-space(.)='로그인 / 회원가입' "
            "or .//*[normalize-space(.)='로그인 / 회원가입']"
            "]"
        ),
    )

    return any(
        button.is_displayed()
        for button in login_buttons
    )


def ensure_panda_logged_in(driver: WebDriver) -> None:
    """로그아웃 상태에서는 랭킹 작업을 진행하지 않는다."""
    if is_panda_logged_out(driver):
        raise RuntimeError(
            f"팬더티비 화면에 '{LOGIN_BUTTON_TEXT}' 문구가 보입니다."
        )


def post_form_with_browser(
        driver: WebDriver,
        url: str,
        form_data: Dict[str, Any],
        timeout_sec: int = 30,
) -> Dict[str, Any]:
    """로그인된 브라우저 세션에서 POST API를 호출한다."""
    driver.set_script_timeout(timeout_sec)
    encoded_body = urlencode(form_data)

    script = """
        const url = arguments[0];
        const body = arguments[1];
        const done = arguments[arguments.length - 1];

        fetch(url, {
            method: "POST",
            credentials: "include",
            headers: {
                "Content-Type":
                    "application/x-www-form-urlencoded; charset=UTF-8"
            },
            body: body
        })
        .then(async response => {
            const responseText = await response.text();

            let responseData = null;
            try {
                responseData = JSON.parse(responseText);
            } catch (e) {
                responseData = null;
            }

            done({
                ok: response.ok,
                status: response.status,
                statusText: response.statusText,
                data: responseData,
                text: responseText
            });
        })
        .catch(error => {
            done({
                ok: false,
                status: 0,
                statusText: "",
                data: null,
                text: "",
                error: String(error)
            });
        });
    """

    result = driver.execute_async_script(
        script,
        url,
        encoded_body,
    )

    if not isinstance(result, dict):
        raise RuntimeError(
            f"API 응답 형식이 올바르지 않습니다: {result}"
        )

    if not result.get("ok"):
        status = result.get("status")
        response_data = result.get("data")

        if isinstance(response_data, dict):
            error_message = (
                    response_data.get("message")
                    or json.dumps(
                response_data,
                ensure_ascii=False,
            )
            )
        else:
            error_message = (
                    result.get("error")
                    or result.get("text")
                    or result.get("statusText")
                    or "알 수 없는 오류"
            )

        raise RuntimeError(
            "랭킹 API 요청 실패: "
            f"status={status}, message={error_message}"
        )

    data = result.get("data")

    if not isinstance(data, dict):
        raise RuntimeError(
            "API 응답이 JSON 객체가 아닙니다: "
            f"{result.get('text', '')[:500]}"
        )

    # HTTP 상태가 200이어도 API가 result=false를 반환할 수 있다.
    if data.get("result") is False:
        error_message = (
                data.get("message")
                or json.dumps(
            data,
            ensure_ascii=False,
        )
        )
        raise RuntimeError(
            f"API 요청 실패: status={result.get('status')}, "
            f"message={error_message}"
        )

    return data


def move_to_job_ranking_page(
        driver: WebDriver,
        selenium_utils: SeleniumUtils,
        ranking_type: str,
        logger,
) -> None:
    """작업 설정에 맞는 랭킹 페이지로 이동한다."""
    ranking_url = RANKING_PAGE_URLS.get(
        ranking_type
    )

    if not ranking_url:
        raise ValueError(
            f"지원하지 않는 ranking_type입니다: {ranking_type}"
        )

    logger.info(
        "[PANDA] 작업 랭킹 페이지 이동 | type=%s",
        ranking_type,
    )

    driver.get(ranking_url)
    selenium_utils.wait_ready_state_complete(
        timeout_sec=15,
    )

    ensure_panda_logged_in(driver)

    logger.info(
        "[PANDA] 작업 랭킹 페이지 이동 완료 | URL=%s",
        driver.current_url,
    )


def fetch_ranking_page(
        driver: WebDriver,
        ranking_type: str,
        page_number: int,
) -> List[Dict[str, Any]]:
    """랭킹 한 페이지를 실제 API로 조회한다."""
    ensure_panda_logged_in(driver)

    payload = {
        "type": ranking_type,
        "limit": PAGE_SIZE,
        "offset": (page_number - 1) * PAGE_SIZE,
    }

    response_json = post_form_with_browser(
        driver=driver,
        url=RANKING_API_URL,
        form_data=payload,
    )

    items = response_json.get(
        "list",
        [],
    )

    if not isinstance(items, list):
        raise RuntimeError(
            f"{page_number}페이지 응답의 list가 배열이 아닙니다."
        )

    return [
        item
        for item in items
        if isinstance(item, dict)
    ]


def fetch_rankings(
        driver: WebDriver,
        job: ExecutionJob,
        logger,
) -> List[Dict[str, Any]]:
    """설정된 시작~종료 페이지를 실제 조회한다."""
    all_items: List[Dict[str, Any]] = []

    for page_number in range(
            job.setting.start_page,
            job.setting.end_page + 1,
    ):
        page_items = fetch_ranking_page(
            driver=driver,
            ranking_type=job.setting.ranking_type,
            page_number=page_number,
        )

        logger.info(
            "[RANKING] HIST_ID=%s | page=%s | 조회=%s",
            job.hist_id,
            page_number,
            len(page_items),
        )

        if not page_items:
            break

        for item in page_items:
            normalized_item = dict(item)
            normalized_item["_source_page"] = page_number
            all_items.append(normalized_item)

        if page_number < job.setting.end_page:
            time.sleep(0.5)

    if not all_items:
        raise RuntimeError(
            "설정된 페이지 범위에서 랭킹 데이터가 조회되지 않았습니다."
        )

    return all_items


def remove_duplicates(
        items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """같은 실행 내에서 userIdx 중복을 제거한다."""
    unique_by_user_idx: Dict[int, Dict[str, Any]] = {}
    no_user_idx_items: List[Dict[str, Any]] = []

    for item in items:
        user_idx = item.get("userIdx")

        if user_idx is None:
            no_user_idx_items.append(item)
            continue

        try:
            normalized_user_idx = int(user_idx)
        except (TypeError, ValueError):
            no_user_idx_items.append(item)
            continue

        if normalized_user_idx not in unique_by_user_idx:
            unique_by_user_idx[normalized_user_idx] = item

    result = list(unique_by_user_idx.values())
    result.extend(no_user_idx_items)

    return result


def build_result_data(
        item: Dict[str, Any],
        index: int,
        job: ExecutionJob,
        status: str,
        result_message: str,
) -> Dict[str, Any]:
    """SERVICE_EXECUTION_RESULT.RESULT_JSON에 저장할 내용을 만든다."""
    source_item = {
        key: value
        for key, value in item.items()
        if not str(key).startswith("_")
    }

    return {
        "dry_run": False,
        "actual_send_yn": False,
        "send_attempted_yn": False,
        "send_success_yn": None,
        "sent_message": None,
        "send_response": None,
        "sequence": index,
        "ranking_type": job.setting.ranking_type,
        "source_page": item.get("_source_page"),
        "message_text": job.setting.message_text,
        "status": status,
        "result_message": result_message,
        "target": {
            "userIdx": item.get("userIdx"),
            "userId": item.get("userId"),
            "userNick": item.get("userNick"),
            "channelTitle": item.get("channelTitle"),
        },
        "source_item": source_item,
    }


def insert_all_results_first(
        items: List[Dict[str, Any]],
        job: ExecutionJob,
        logger,
        result_inserter: ResultInserter,
) -> tuple[List[PreparedResult], int]:
    """
    발송 전에 모든 상세 결과를 먼저 INSERT한다.

    userIdx가 있는 대상은 READY,
    userIdx가 없는 대상은 SKIP으로 저장한다.
    """
    prepared_results: List[PreparedResult] = []
    skipped_count = 0
    total_count = len(items)

    logger.info(
        "[RESULT] 상세 결과 선INSERT 시작 | HIST_ID=%s | 대상=%s",
        job.hist_id,
        total_count,
    )

    for index, item in enumerate(
            items,
            start=1,
    ):
        user_idx = item.get("userIdx")

        if user_idx is None:
            status = "SKIP"
            result_message = (
                "userIdx가 없어 메시지 발송 대상에서 제외했습니다."
            )
        else:
            status = "READY"
            result_message = (
                "상세 결과 INSERT 완료, 메시지 발송 대기 상태입니다."
            )

        result_data = build_result_data(
            item=item,
            index=index,
            job=job,
            status=status,
            result_message=result_message,
        )

        result_id = result_inserter(
            status,
            result_data,
            None,
        )

        if status == "SKIP":
            skipped_count += 1
        else:
            prepared_results.append(
                PreparedResult(
                    result_id=result_id,
                    result_data=result_data,
                )
            )

        if (
                index % 20 == 0
                or index == total_count
        ):
            logger.info(
                "[RESULT] HIST_ID=%s | 선INSERT=%s/%s | "
                "READY=%s | SKIP=%s",
                job.hist_id,
                index,
                total_count,
                len(prepared_results),
                skipped_count,
            )

    logger.info(
        "[RESULT] 모든 상세 결과 INSERT 완료 "
        "| HIST_ID=%s | READY=%s | SKIP=%s",
        job.hist_id,
        len(prepared_results),
        skipped_count,
    )

    return prepared_results, skipped_count


def generate_message(message_template: str) -> str:
    """
    DB의 message_text로 실제 발송 문구를 만든다.

    - 문구에 @가 있으면 @를 [영문·숫자 4자리]로 치환
    - @가 없으면 문구 끝에 [영문·숫자 4자리]를 추가
    """
    random_code = "".join(
        random.choices(
            string.ascii_letters + string.digits,
            k=4,
            )
    )
    random_suffix = f"[{random_code}]"

    if "@" in message_template:
        return message_template.replace(
            "@",
            random_suffix,
        )

    return f"{message_template} {random_suffix}".strip()


def build_updated_result_data(
        prepared: PreparedResult,
        status: str,
        result_message: str,
        sent_message: Optional[str],
        send_attempted_yn: bool,
        send_success_yn: Optional[bool],
        send_response: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """READY로 INSERT된 RESULT_JSON을 발송 결과에 맞게 갱신한다."""
    updated_data = dict(
        prepared.result_data
    )

    updated_data["status"] = status
    updated_data["result_message"] = result_message
    updated_data["actual_send_yn"] = (
        send_attempted_yn
    )
    updated_data["send_attempted_yn"] = (
        send_attempted_yn
    )
    updated_data["send_success_yn"] = (
        send_success_yn
    )
    updated_data["sent_message"] = (
        sent_message
    )
    updated_data["send_response"] = (
        send_response
    )

    return updated_data


def skip_remaining_results(
        remaining_results: List[PreparedResult],
        reason: str,
        result_updater: ResultUpdater,
        logger,
        job: ExecutionJob,
) -> int:
    """전송 제한 등으로 중단할 때 아직 발송하지 않은 READY 결과를 SKIP 처리한다."""
    skipped_count = 0

    for prepared in remaining_results:
        updated_data = build_updated_result_data(
            prepared=prepared,
            status="SKIP",
            result_message=reason,
            sent_message=None,
            send_attempted_yn=False,
            send_success_yn=None,
            send_response=None,
        )

        result_updater(
            prepared.result_id,
            "SKIP",
            updated_data,
            reason,
        )

        skipped_count += 1

    if skipped_count > 0:
        logger.warning(
            "[MESSAGE] HIST_ID=%s | 남은 대상 SKIP=%s | reason=%s",
            job.hist_id,
            skipped_count,
            reason,
        )

    return skipped_count


def send_messages_after_insert(
        driver: WebDriver,
        prepared_results: List[PreparedResult],
        job: ExecutionJob,
        logger,
        result_updater: ResultUpdater,
) -> tuple[int, int, int, bool, Optional[str]]:
    """
    모든 상세 결과 INSERT가 끝난 뒤 실제 쪽지를 발송한다.

    - 성공: READY -> SUCCESS
    - 개별 실패: READY -> FAIL 후 다음 대상 계속 진행
    - 쪽지 발송 제한: 현재 대상 FAIL, 남은 대상 SKIP 후 즉시 중단
    """
    success_count = 0
    fail_count = 0
    additional_skip_count = 0
    stopped_early = False
    stop_reason: Optional[str] = None

    total_count = len(
        prepared_results
    )

    logger.info(
        "[MESSAGE] 선INSERT 완료 후 실제 쪽지 발송 시작 "
        "| HIST_ID=%s | 대상=%s | URL=%s",
        job.hist_id,
        total_count,
        SEND_MESSAGE_API_URL,
    )

    for index, prepared in enumerate(
            prepared_results,
            start=1,
    ):
        target = prepared.result_data.get(
            "target",
            {},
        )
        user_idx = target.get("userIdx")
        user_nick = target.get(
            "userNick",
            "",
        )

        sent_message = generate_message(
            job.setting.message_text
        )

        payload = {
            "message": sent_message,
            "userIdx": user_idx,
        }

        try:
            # 발송 직전에도 로그인 상태를 확인한다.
            ensure_panda_logged_in(driver)

            response_json = post_form_with_browser(
                driver=driver,
                url=SEND_MESSAGE_API_URL,
                form_data=payload,
            )

            updated_data = build_updated_result_data(
                prepared=prepared,
                status="SUCCESS",
                result_message="메시지 발송에 성공했습니다.",
                sent_message=sent_message,
                send_attempted_yn=True,
                send_success_yn=True,
                send_response=response_json,
            )

            result_updater(
                prepared.result_id,
                "SUCCESS",
                updated_data,
                None,
            )

            success_count += 1

            logger.info(
                "[MESSAGE] HIST_ID=%s | %s/%s | SUCCESS "
                "| userNick=%s | userIdx=%s",
                job.hist_id,
                index,
                total_count,
                user_nick,
                user_idx,
            )

        except Exception as error:
            error_message = str(error)

            updated_data = build_updated_result_data(
                prepared=prepared,
                status="FAIL",
                result_message="메시지 발송에 실패했습니다.",
                sent_message=sent_message,
                send_attempted_yn=True,
                send_success_yn=False,
                send_response=None,
            )

            result_updater(
                prepared.result_id,
                "FAIL",
                updated_data,
                error_message,
            )

            fail_count += 1

            logger.error(
                "[MESSAGE] HIST_ID=%s | %s/%s | FAIL "
                "| userNick=%s | userIdx=%s | ERROR=%s",
                job.hist_id,
                index,
                total_count,
                user_nick,
                user_idx,
                error_message,
            )

            # 계정 발송 제한은 이후 대상도 실패할 가능성이 높으므로 즉시 중단한다.
            if MESSAGE_LIMIT_TEXT in error_message:
                stopped_early = True
                stop_reason = error_message

                remaining_results = prepared_results[
                                    index:
                                    ]

                additional_skip_count = skip_remaining_results(
                    remaining_results=remaining_results,
                    reason=(
                        "계정의 쪽지 발송 제한으로 "
                        "남은 대상을 발송하지 않았습니다."
                    ),
                    result_updater=result_updater,
                    logger=logger,
                    job=job,
                )

                logger.error(
                    "[MESSAGE] 발송 제한으로 작업 중단 "
                    "| HIST_ID=%s | SUCCESS=%s | FAIL=%s | SKIP=%s",
                    job.hist_id,
                    success_count,
                    fail_count,
                    additional_skip_count,
                )
                break

        # 마지막 대상이 아니고 중단되지 않았을 때만 랜덤 대기한다.
        if (
                index < total_count
                and not stopped_early
        ):
            delay = random.uniform(
                MESSAGE_SEND_DELAY_MIN_SEC,
                MESSAGE_SEND_DELAY_MAX_SEC,
            )

            logger.info(
                "[MESSAGE] 다음 발송까지 %.2f초 대기합니다.",
                delay,
            )
            time.sleep(delay)

    logger.info(
        "[MESSAGE] 실제 쪽지 발송 완료 "
        "| HIST_ID=%s | SUCCESS=%s | FAIL=%s | 추가SKIP=%s",
        job.hist_id,
        success_count,
        fail_count,
        additional_skip_count,
    )

    return (
        success_count,
        fail_count,
        additional_skip_count,
        stopped_early,
        stop_reason,
    )


def execute_message_job(
        driver: WebDriver,
        selenium_utils: SeleniumUtils,
        job: ExecutionJob,
        logger,
        result_inserter: ResultInserter,
        result_updater: ResultUpdater,
) -> MessageExecutionResult:
    """랭킹 조회 → 전체 선INSERT → 실제 쪽지 발송 순서로 실행한다."""
    move_to_job_ranking_page(
        driver=driver,
        selenium_utils=selenium_utils,
        ranking_type=job.setting.ranking_type,
        logger=logger,
    )

    fetched_items = fetch_rankings(
        driver=driver,
        job=job,
        logger=logger,
    )

    if job.setting.exclude_duplicate_yn:
        target_items = remove_duplicates(
            fetched_items
        )
    else:
        target_items = list(
            fetched_items
        )

    logger.info(
        "[RANKING] HIST_ID=%s | 전체조회=%s | 최종대상=%s",
        job.hist_id,
        len(fetched_items),
        len(target_items),
    )

    # 1단계: 모든 결과를 READY 또는 SKIP으로 먼저 INSERT한다.
    prepared_results, initial_skip_count = insert_all_results_first(
        items=target_items,
        job=job,
        logger=logger,
        result_inserter=result_inserter,
    )

    # 2단계: 선INSERT가 모두 완료된 경우에만 실제 발송을 시작한다.
    (
        success_count,
        fail_count,
        additional_skip_count,
        stopped_early,
        stop_reason,
    ) = send_messages_after_insert(
        driver=driver,
        prepared_results=prepared_results,
        job=job,
        logger=logger,
        result_updater=result_updater,
    )

    return MessageExecutionResult(
        fetched_count=len(fetched_items),
        target_count=len(target_items),
        inserted_count=len(target_items),
        success_count=success_count,
        fail_count=fail_count,
        skipped_count=(
                initial_skip_count
                + additional_skip_count
        ),
        stopped_early=stopped_early,
        stop_reason=stop_reason,
    )