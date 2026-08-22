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
5. 계정을 라운드로빈으로 순환하며 발송
6. 한 계정이 제한되면 같은 대상을 다음 계정으로 재시도
7. 모든 계정이 제한되면 현재 대상 FAIL, 남은 READY 대상 SKIP 후 중단
"""

import json
import random
import string
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.database.execution_repository import (
    ExecutionJob,
    PandaLoginAccount,
)
from src.utils.selenium_utils import SeleniumUtils


# ============================================================
# 팬더라이브 쪽지 발송 API 응답 / 데몬 처리 기준
# ============================================================
#
# [HTTP 400] 발신 계정 쪽지 전송 제한
#
# 확인된 message:
#   쪽지 전송이 제한되었습니다. 잠시 후 다시 시도해 주세요.
#
# 판별 문자열:
#   "쪽지 전송이 제한되었습니다"
#
# 데몬 처리:
#   - 현재 발신 계정은 더 이상 사용하지 않음
#   - 같은 대상을 다음 로그인 계정으로 즉시 재시도
#   - 다음 계정에서 성공하면 이후 대상도 해당 계정으로 계속 발송
#   - 모든 로그인 계정에서 동일한 전송 제한이 발생하면
#     60분 대기 후 같은 대상부터 전체 계정을 1회 재확인
#   - 재확인에서도 모든 계정이 제한이면
#     현재 대상 FAIL, 남은 대상 SKIP 후 작업 종료
#
#
# [HTTP 400] 상대 BJ 쪽지 수신 제한
#
# 확인된 message 예시:
#   BJ님께서 브론즈(30일) 등급 아래인 경우
#   쪽지 수신을 제한하여 발송이 불가능합니다.
#
#   BJ님께서 감색 등급 아래인 경우
#   쪽지 수신을 제한하여 발송이 불가능합니다.
#
# 판별 문자열:
#   "쪽지 수신을 제한"
#
# 데몬 처리:
#   - 상대 BJ의 수신 설정에 의한 제한으로 판단
#   - 다른 발신 계정으로 재시도하지 않음
#   - 현재 대상 FAIL
#   - 현재 로그인 계정은 그대로 유지
#   - 다음 대상으로 계속 진행
#
#
# [HTTP 200] 정상 발송
#
# 데몬 처리:
#   - SUCCESS
#   - 현재 로그인 계정을 그대로 유지
#
# 데몬 결과 표시:
#   - 메시지 발송에 성공했습니다.
#
# 참고:
#   성공 응답 JSON의 실제 message/result 전체 구조는 현재 확인된 로그만으로
#   특정 문자열 판별 기준을 만들지 않고 HTTP/API 정상 여부로 처리한다.
# ============================================================

RANKING_API_URL = "https://api.pandalive.co.kr/v1/live/cache"
SEND_MESSAGE_API_URL = "https://api.pandalive.co.kr/v1/post/send_message"

PANDA_LIVE_URL = "https://www.pandalive.co.kr/"

LOGIN_BUTTON_TEXT = "로그인 / 회원가입"
MESSAGE_LIMIT_TEXT = "쪽지 전송이 제한되었습니다"
MESSAGE_RECEIVE_LIMIT_TEXT = "쪽지 수신을 제한"

# 자동 로그인 대기 시간
AD_POPUP_WAIT_SEC = 3
LOGIN_ELEMENT_WAIT_SEC = 10
LOGIN_RESULT_WAIT_SEC = 20

PAGE_SIZE = 20

# 실제 쪽지 발송 사이의 랜덤 대기 시간
MESSAGE_SEND_DELAY_MIN_SEC = 20
MESSAGE_SEND_DELAY_MAX_SEC = 30

# 모든 발신 계정이 전송 제한일 때 대기 시간
MESSAGE_LIMIT_COOLDOWN_SEC = 60 * 60

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



def _visible_elements(
        driver: WebDriver,
        xpath: str,
) -> List[Any]:
    """XPath와 일치하는 요소 중 화면에 보이는 요소만 반환한다."""
    return [
        element
        for element in driver.find_elements(By.XPATH, xpath)
        if element.is_displayed()
    ]


def _click_element(driver: WebDriver, element: Any) -> None:
    """일반 클릭이 막히면 JavaScript 클릭으로 한 번 더 시도한다."""
    try:
        element.click()
    except Exception:
        driver.execute_script(
            "arguments[0].click();",
            element,
        )


def close_ad_popup_if_present(
        driver: WebDriver,
        logger,
) -> None:
    """
    메인 접속 후 광고 팝업의 닫기 버튼을 최대 3초 동안 찾는다.

    광고가 없으면 3초 대기 후 로그인 단계를 계속한다.
    """
    close_xpath = (
        "//*[@data-modal='useBannerViewModel']"
        "//button[normalize-space(.)='닫기']"
    )

    try:
        close_button = WebDriverWait(
            driver,
            AD_POPUP_WAIT_SEC,
        ).until(
            EC.element_to_be_clickable(
                (By.XPATH, close_xpath)
            )
        )

        _click_element(driver, close_button)

        try:
            WebDriverWait(driver, 3).until(
                EC.invisibility_of_element_located(
                    (By.XPATH, close_xpath)
                )
            )
        except TimeoutException:
            pass

        logger.info("[LOGIN] 광고 팝업을 닫았습니다.")

    except TimeoutException:
        logger.info(
            "[LOGIN] 광고 팝업이 없어 %s초 대기 후 로그인을 진행합니다.",
            AD_POPUP_WAIT_SEC,
        )


def click_login_entry(driver: WebDriver, logger) -> None:
    """로그인/회원가입 영역과 이어서 나타나는 로그인 버튼을 누른다."""
    login_entry_xpath = (
        "//*[self::div or self::button]"
        "[normalize-space(.)='로그인 / 회원가입']"
    )

    login_entry = WebDriverWait(
        driver,
        LOGIN_ELEMENT_WAIT_SEC,
    ).until(
        EC.element_to_be_clickable(
            (By.XPATH, login_entry_xpath)
        )
    )

    _click_element(driver, login_entry)
    logger.info("[LOGIN] 로그인 / 회원가입 버튼을 클릭했습니다.")

    tooltip_login_xpath = (
        "//button[@type='button' and not(@role='tab') "
        "and normalize-space(.)='로그인']"
    )
    login_tab_xpath = (
        "//button[@role='tab' and .//p[normalize-space(.)='로그인']]"
    )

    try:
        tooltip_login = WebDriverWait(
            driver,
            5,
        ).until(
            EC.element_to_be_clickable(
                (By.XPATH, tooltip_login_xpath)
            )
        )

        _click_element(driver, tooltip_login)
        logger.info("[LOGIN] 툴팁 로그인 버튼을 클릭했습니다.")

    except TimeoutException:
        # 사이트 상태에 따라 툴팁 없이 로그인 모달이 바로 열릴 수 있다.
        if not _visible_elements(driver, login_tab_xpath):
            raise RuntimeError(
                "툴팁 로그인 버튼 또는 로그인 모달을 찾지 못했습니다."
            )

        logger.info("[LOGIN] 로그인 모달이 바로 열렸습니다.")

    login_tab = WebDriverWait(
        driver,
        LOGIN_ELEMENT_WAIT_SEC,
    ).until(
        EC.element_to_be_clickable(
            (By.XPATH, login_tab_xpath)
        )
    )

    _click_element(driver, login_tab)
    logger.info("[LOGIN] 로그인 탭을 클릭했습니다.")


def fill_login_form(
        driver: WebDriver,
        login_id: str,
        login_password: str,
        logger,
) -> None:
    """작업 설정의 아이디와 비밀번호를 입력하고 로그인한다."""
    id_input = WebDriverWait(
        driver,
        LOGIN_ELEMENT_WAIT_SEC,
    ).until(
        EC.visibility_of_element_located(
            (By.ID, "id")
        )
    )

    password_input = WebDriverWait(
        driver,
        LOGIN_ELEMENT_WAIT_SEC,
    ).until(
        EC.visibility_of_element_located(
            (By.NAME, "pw")
        )
    )

    id_input.click()
    id_input.send_keys(Keys.CONTROL, "a")
    id_input.send_keys(login_id)

    password_input.click()
    password_input.send_keys(Keys.CONTROL, "a")
    password_input.send_keys(login_password)

    login_button = WebDriverWait(
        driver,
        LOGIN_ELEMENT_WAIT_SEC,
    ).until(
        EC.element_to_be_clickable(
            (By.ID, "login-button")
        )
    )

    _click_element(driver, login_button)
    logger.info("[LOGIN] 아이디와 비밀번호를 입력하고 로그인 버튼을 클릭했습니다.")


def reset_panda_login_session(
        driver: WebDriver,
        logger,
) -> None:
    """계정 변경/재로그인 전에 기존 팬더 세션을 초기화한다."""
    logger.info("[LOGIN] 기존 팬더 로그인 세션을 초기화합니다.")

    try:
        driver.delete_all_cookies()
    except Exception as error:
        logger.warning("[LOGIN] 쿠키 초기화 실패: %s", error)

    try:
        driver.get(PANDA_LIVE_URL)
        WebDriverWait(driver, 10).until(
            lambda current_driver: current_driver.execute_script(
                "return document.readyState"
            ) == "complete"
        )
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            try { window.localStorage.clear(); } catch (e) {}
            try { window.sessionStorage.clear(); } catch (e) {}
            """
        )
    except Exception:
        pass

    try:
        driver.delete_all_cookies()
    except Exception:
        pass


def login_panda_with_retry(
        driver: WebDriver,
        selenium_utils: SeleniumUtils,
        account: PandaLoginAccount,
        logger,
) -> None:
    """로그인 전체 과정을 최대 2회(최초 1회 + 재시도 1회) 수행한다."""
    max_attempts = 2

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(
                "[LOGIN] 팬더 로그인 시작 | login_id=%s | attempt=%s/%s",
                account.login_id,
                attempt,
                max_attempts,
            )

            reset_panda_login_session(
                driver=driver,
                logger=logger,
            )

            login_panda(
                driver=driver,
                selenium_utils=selenium_utils,
                login_id=account.login_id,
                login_password=account.login_password,
                logger=logger,
            )

            logger.info(
                "[LOGIN] 팬더 로그인 완료 | login_id=%s",
                account.login_id,
            )
            return

        except Exception as error:
            logger.warning(
                "[LOGIN] 팬더 로그인 실패 | login_id=%s | attempt=%s/%s | ERROR=%s",
                account.login_id,
                attempt,
                max_attempts,
                error,
            )

            if attempt >= max_attempts:
                raise RuntimeError(
                    "팬더 로그인에 최종 실패했습니다. "
                    f"login_id={account.login_id}, error={error}"
                ) from error

            logger.info(
                "[LOGIN] 로그인 전체 과정을 1회 다시 시도합니다. | login_id=%s",
                account.login_id,
            )
            time.sleep(1)


def is_message_limit_error(error: Exception) -> bool:
    """발신 계정의 쪽지 전송 제한 오류인지 확인한다."""
    return MESSAGE_LIMIT_TEXT in str(error)


def is_message_receive_limit_error(error: Exception) -> bool:
    """상대 BJ의 쪽지 수신 제한 오류인지 확인한다."""
    return MESSAGE_RECEIVE_LIMIT_TEXT in str(error)


def restart_panda_browser(
        selenium_utils: SeleniumUtils,
        logger,
) -> WebDriver:
    """발신 계정 변경 전에 기존 Chrome을 완전히 종료하고 새 Chrome을 실행한다."""
    logger.info("[SELENIUM] 계정 변경을 위해 기존 Chrome 브라우저를 종료합니다.")

    selenium_utils.quit()

    logger.info("[SELENIUM] 새 Chrome 브라우저를 실행합니다.")

    new_driver = selenium_utils.start_driver(
        timeout=30,
        view_mode="browser",
        window_size=(1200, 900),
    )

    logger.info("[SELENIUM] 계정 변경용 Chrome 브라우저 재실행 완료")

    return new_driver


def login_panda(
        driver: WebDriver,
        selenium_utils: SeleniumUtils,
        login_id: str,
        login_password: str,
        logger,
) -> None:
    """팬더라이브 접속부터 자동 로그인 완료 확인까지 처리한다."""
    logger.info("[PANDA] 팬더라이브에 접속합니다.")

    driver.get(PANDA_LIVE_URL)
    selenium_utils.wait_ready_state_complete(
        timeout_sec=15,
    )

    logger.info(
        "[PANDA] 접속 완료 | URL=%s | TITLE=%s",
        driver.current_url,
        driver.title,
    )

    close_ad_popup_if_present(
        driver=driver,
        logger=logger,
    )

    click_login_entry(
        driver=driver,
        logger=logger,
    )

    fill_login_form(
        driver=driver,
        login_id=login_id,
        login_password=login_password,
        logger=logger,
    )

    try:
        WebDriverWait(
            driver,
            LOGIN_RESULT_WAIT_SEC,
        ).until(
            lambda current_driver: not is_panda_logged_out(
                current_driver
            )
        )

    except TimeoutException as error:
        raise RuntimeError(
            "팬더라이브 로그인 완료를 확인하지 못했습니다. "
            "아이디 또는 비밀번호를 확인해 주세요."
        ) from error

    logger.info(
        "[LOGIN] 팬더라이브 자동 로그인 성공 | login_id=%s",
        login_id,
    )

def is_panda_logged_out(driver: WebDriver) -> bool:
    """화면에 '로그인 / 회원가입' 영역이 보이면 로그아웃으로 판단한다."""
    login_elements = driver.find_elements(
        By.XPATH,
        (
            "//*[self::div or self::button]"
            "[normalize-space(.)='로그인 / 회원가입']"
        ),
    )

    return any(
        element.is_displayed()
        for element in login_elements
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

        for page_item_index, item in enumerate(page_items, start=1):
            normalized_item = dict(item)
            normalized_item["_source_page"] = page_number
            normalized_item["_source_rank"] = (
                    (page_number - 1) * PAGE_SIZE
                    + page_item_index
            )
            all_items.append(normalized_item)

        if page_number < job.setting.end_page:
            time.sleep(0.5)

    if not all_items:
        raise RuntimeError(
            "설정된 페이지 범위에서 랭킹 데이터가 조회되지 않았습니다."
        )

    return all_items


def get_rank_range(job: ExecutionJob) -> tuple[int, int]:
    """
    SETTING_JSON에서 시작순위/종료순위를 읽는다.

    execution_repository.py를 수정하지 않도록 ExecutionJob.setting_json에서
    직접 읽는다. 값이 없는 기존 예약 데이터는 선택 페이지 전체 범위를
    기본 순위 범위로 사용한다.
    """
    default_start_rank = (
            (job.setting.start_page - 1) * PAGE_SIZE
            + 1
    )
    default_end_rank = (
            job.setting.end_page * PAGE_SIZE
    )

    try:
        raw_settings = json.loads(job.setting_json or "{}")
    except (TypeError, json.JSONDecodeError):
        raw_settings = {}

    if not isinstance(raw_settings, dict):
        raw_settings = {}

    fields = raw_settings.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue

            code = str(field.get("code") or "").strip()
            if not code:
                continue

            raw_settings[code] = field.get("value")

    try:
        start_rank = int(
            raw_settings.get(
                "start_rank",
                default_start_rank,
            )
        )
        end_rank = int(
            raw_settings.get(
                "end_rank",
                default_end_rank,
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "start_rank와 end_rank는 숫자여야 합니다."
        ) from error

    if start_rank < 1:
        raise ValueError(
            "start_rank는 1 이상이어야 합니다."
        )

    if end_rank < start_rank:
        raise ValueError(
            "end_rank는 start_rank보다 크거나 같아야 합니다."
        )

    return start_rank, end_rank


def filter_rank_range(
        items: List[Dict[str, Any]],
        start_rank: int,
        end_rank: int,
) -> List[Dict[str, Any]]:
    """조회한 랭킹 중 절대 순위가 시작순위~종료순위인 대상만 남긴다."""
    return [
        item
        for item in items
        if start_rank
           <= int(item.get("_source_rank") or 0)
           <= end_rank
    ]


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
        "source_rank": item.get("_source_rank"),
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
        selenium_utils: SeleniumUtils,
        prepared_results: List[PreparedResult],
        job: ExecutionJob,
        logger,
        result_updater: ResultUpdater,
        initial_account_index: int = 0,
) -> tuple[int, int, int, bool, Optional[str]]:
    """
    계정을 라운드로빈으로 순환하며 실제 쪽지를 발송한다.

    - 정상: 현재 로그인 계정으로 계속 발송
    - 발신 전송 제한: Chrome을 완전히 재시작한 뒤 같은 대상을 다음 계정으로 즉시 재시도
    - 다음 계정에서 성공하면 이후 대상도 해당 계정을 계속 사용
    - 상대 수신 제한: 현재 대상 FAIL 후 현재 계정을 유지하고 다음 대상으로 진행
    - 같은 대상에서 모든 계정 제한: 60분 대기 후 같은 대상부터 전체 계정 1회 재확인
    - 재확인에서도 모든 계정 제한: 현재 대상 FAIL, 남은 대상 SKIP, 작업 중단
    - 기타 개별 오류: 현재 대상 FAIL 후 현재 계정을 유지하고 다음 대상으로 진행
    """
    success_count = 0
    fail_count = 0
    additional_skip_count = 0
    stopped_early = False
    stop_reason: Optional[str] = None

    accounts = list(job.setting.login_accounts)
    if not accounts:
        raise RuntimeError("사용 가능한 팬더 로그인 계정이 없습니다.")

    account_count = len(accounts)
    active_account_index = initial_account_index
    round_robin_index = initial_account_index
    total_count = len(prepared_results)

    logger.info(
        "[MESSAGE] 선INSERT 완료 후 실제 쪽지 발송 시작 "
        "| HIST_ID=%s | 대상=%s | 계정수=%s | URL=%s",
        job.hist_id,
        total_count,
        account_count,
        SEND_MESSAGE_API_URL,
    )

    for target_index, prepared in enumerate(prepared_results, start=1):
        target = prepared.result_data.get("target", {})
        user_idx = target.get("userIdx")
        user_nick = target.get("userNick", "")

        payload = {
            "message": job.setting.message_text,
            "userIdx": user_idx,
        }

        candidate_account_index = round_robin_index % account_count
        limited_account_count = 0
        attempted_account_ids: List[str] = []
        target_completed = False
        cooldown_retry_used = False

        while limited_account_count < account_count:
            account = accounts[candidate_account_index]
            attempted_account_ids.append(account.login_id)

            try:
                if active_account_index != candidate_account_index:
                    logger.info(
                        "[ACCOUNT] 계정 변경 | HIST_ID=%s | target=%s/%s | login_id=%s",
                        job.hist_id,
                        target_index,
                        total_count,
                        account.login_id,
                    )

                    driver = restart_panda_browser(
                        selenium_utils=selenium_utils,
                        logger=logger,
                    )

                    login_panda_with_retry(
                        driver=driver,
                        selenium_utils=selenium_utils,
                        account=account,
                        logger=logger,
                    )
                    active_account_index = candidate_account_index

                ensure_panda_logged_in(driver)

                logger.info(
                    "[MESSAGE] 발송 시도 | HIST_ID=%s | %s/%s "
                    "| login_id=%s | userNick=%s | userIdx=%s",
                    job.hist_id,
                    target_index,
                    total_count,
                    account.login_id,
                    user_nick,
                    user_idx,
                )

                response_json = post_form_with_browser(
                    driver=driver,
                    url=SEND_MESSAGE_API_URL,
                    form_data=payload,
                )

                updated_data = build_updated_result_data(
                    prepared=prepared,
                    status="SUCCESS",
                    result_message="메시지 발송에 성공했습니다.",
                    sent_message=job.setting.message_text,
                    send_attempted_yn=True,
                    send_success_yn=True,
                    send_response=response_json,
                )
                updated_data["send_account_id"] = account.login_id

                result_updater(
                    prepared.result_id,
                    "SUCCESS",
                    updated_data,
                    None,
                )

                success_count += 1
                target_completed = True
                round_robin_index = candidate_account_index

                logger.info(
                    "[MESSAGE] HIST_ID=%s | %s/%s | SUCCESS "
                    "| login_id=%s | userNick=%s | userIdx=%s",
                    job.hist_id,
                    target_index,
                    total_count,
                    account.login_id,
                    user_nick,
                    user_idx,
                )
                break

            except Exception as error:
                error_message = str(error)

                # 상대 BJ가 쪽지 수신을 제한한 경우에는
                # 발신 계정을 변경해도 동일하므로 현재 대상만 FAIL 처리하고
                # 다음 대상으로 계속 진행한다.
                if is_message_receive_limit_error(error):
                    updated_data = build_updated_result_data(
                        prepared=prepared,
                        status="FAIL",
                        result_message=(
                            "상대 BJ가 쪽지 수신을 제한하여 "
                            "발송할 수 없습니다."
                        ),
                        sent_message=job.setting.message_text,
                        send_attempted_yn=True,
                        send_success_yn=False,
                        send_response=None,
                    )
                    updated_data["send_account_id"] = account.login_id

                    result_updater(
                        prepared.result_id,
                        "FAIL",
                        updated_data,
                        error_message,
                    )

                    fail_count += 1
                    target_completed = True
                    round_robin_index = candidate_account_index

                    logger.warning(
                        "[MESSAGE] 상대 BJ 쪽지 수신 제한 | HIST_ID=%s | %s/%s "
                        "| login_id=%s | userNick=%s | userIdx=%s | ERROR=%s",
                        job.hist_id,
                        target_index,
                        total_count,
                        account.login_id,
                        user_nick,
                        user_idx,
                        error_message,
                    )
                    break

                if is_message_limit_error(error):
                    limited_account_count += 1

                    logger.warning(
                        "[MESSAGE] 계정 쪽지 전송 제한 | HIST_ID=%s | target=%s/%s "
                        "| login_id=%s | 제한계정=%s/%s | ERROR=%s",
                        job.hist_id,
                        target_index,
                        total_count,
                        account.login_id,
                        limited_account_count,
                        account_count,
                        error_message,
                    )

                    if limited_account_count >= account_count:
                        if not cooldown_retry_used:
                            cooldown_retry_used = True

                            logger.warning(
                                "[MESSAGE] 모든 계정 전송 제한 확인 | HIST_ID=%s "
                                "| target=%s/%s | 60분 대기 후 같은 대상 재확인",
                                job.hist_id,
                                target_index,
                                total_count,
                            )

                            time.sleep(MESSAGE_LIMIT_COOLDOWN_SEC)

                            # 60분 후 실패했던 같은 대상부터 전체 계정을 다시 1회 확인한다.
                            limited_account_count = 0
                            attempted_account_ids = []
                            candidate_account_index = round_robin_index % account_count

                            logger.info(
                                "[MESSAGE] 전송 제한 재확인 시작 | HIST_ID=%s "
                                "| target=%s/%s | first_login_id=%s",
                                job.hist_id,
                                target_index,
                                total_count,
                                accounts[candidate_account_index].login_id,
                            )
                            continue

                        stop_reason = (
                            "데몬: 60분 대기 후 재확인했지만 사용 가능한 모든 "
                            "팬더 계정에서 쪽지 전송 제한이 계속 확인되었습니다. "
                            f"accounts={','.join(attempted_account_ids)}"
                        )

                        updated_data = build_updated_result_data(
                            prepared=prepared,
                            status="FAIL",
                            result_message=stop_reason,
                            sent_message=job.setting.message_text,
                            send_attempted_yn=True,
                            send_success_yn=False,
                            send_response=None,
                        )
                        updated_data["send_account_id"] = account.login_id

                        result_updater(
                            prepared.result_id,
                            "FAIL",
                            updated_data,
                            stop_reason,
                        )

                        fail_count += 1
                        stopped_early = True

                        remaining_results = prepared_results[target_index:]
                        additional_skip_count = skip_remaining_results(
                            remaining_results=remaining_results,
                            reason=(
                                "데몬 작업 중단: 60분 대기 후 재확인에서도 "
                                "사용 가능한 발송 계정이 없어 남은 대상을 "
                                "SKIP 처리했습니다."
                            ),
                            result_updater=result_updater,
                            logger=logger,
                            job=job,
                        )

                        logger.error(
                            "[MESSAGE] 60분 대기 후에도 모든 계정 전송 제한 "
                            "| 작업 중단 | HIST_ID=%s | SUCCESS=%s "
                            "| FAIL=%s | SKIP=%s",
                            job.hist_id,
                            success_count,
                            fail_count,
                            additional_skip_count,
                        )
                        break

                    candidate_account_index = (candidate_account_index + 1) % account_count
                    logger.info(
                        "[ACCOUNT] 다음 계정으로 동일 대상 재시도 "
                        "| HIST_ID=%s | next_login_id=%s | userNick=%s",
                        job.hist_id,
                        accounts[candidate_account_index].login_id,
                        user_nick,
                    )
                    continue

                updated_data = build_updated_result_data(
                    prepared=prepared,
                    status="FAIL",
                    result_message="메시지 발송에 실패했습니다.",
                    sent_message=job.setting.message_text,
                    send_attempted_yn=True,
                    send_success_yn=False,
                    send_response=None,
                )
                updated_data["send_account_id"] = account.login_id

                result_updater(
                    prepared.result_id,
                    "FAIL",
                    updated_data,
                    error_message,
                )

                fail_count += 1
                target_completed = True
                round_robin_index = candidate_account_index

                logger.error(
                    "[MESSAGE] HIST_ID=%s | %s/%s | FAIL "
                    "| login_id=%s | userNick=%s | userIdx=%s | ERROR=%s",
                    job.hist_id,
                    target_index,
                    total_count,
                    account.login_id,
                    user_nick,
                    user_idx,
                    error_message,
                )
                break

        if stopped_early:
            break

        if not target_completed:
            raise RuntimeError(
                "메시지 처리 결과가 결정되지 않았습니다. "
                f"userIdx={user_idx}"
            )

        if target_index < total_count:
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
        "| HIST_ID=%s | SUCCESS=%s | FAIL=%s | 추가SKIP=%s | STOPPED=%s",
        job.hist_id,
        success_count,
        fail_count,
        additional_skip_count,
        stopped_early,
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
    """자동 로그인 -> 랭킹 조회 -> 선INSERT -> 계정 순환 발송 순서로 실행한다."""
    accounts = list(job.setting.login_accounts)

    if not accounts:
        raise RuntimeError("팬더 로그인 계정이 없습니다.")

    logger.info(
        "[ACCOUNT] 팬더 메시지 작업 계정수=%s",
        len(accounts),
    )

    first_account = accounts[0]

    login_panda_with_retry(
        driver=driver,
        selenium_utils=selenium_utils,
        account=first_account,
        logger=logger,
    )

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

    start_rank, end_rank = get_rank_range(job)

    ranked_items = filter_rank_range(
        items=fetched_items,
        start_rank=start_rank,
        end_rank=end_rank,
    )

    if not ranked_items:
        raise RuntimeError(
            "설정된 순위 범위에서 랭킹 데이터가 조회되지 않았습니다. "
            f"start_rank={start_rank}, end_rank={end_rank}"
        )

    if job.setting.exclude_duplicate_yn:
        target_items = remove_duplicates(ranked_items)
    else:
        target_items = list(ranked_items)

    logger.info(
        "[RANKING] HIST_ID=%s | 전체조회=%s | 순위범위=%s~%s "
        "| 순위필터후=%s | 최종대상=%s",
        job.hist_id,
        len(fetched_items),
        start_rank,
        end_rank,
        len(ranked_items),
        len(target_items),
    )

    prepared_results, initial_skip_count = insert_all_results_first(
        items=target_items,
        job=job,
        logger=logger,
        result_inserter=result_inserter,
    )

    (
        success_count,
        fail_count,
        additional_skip_count,
        stopped_early,
        stop_reason,
    ) = send_messages_after_insert(
        driver=driver,
        selenium_utils=selenium_utils,
        prepared_results=prepared_results,
        job=job,
        logger=logger,
        result_updater=result_updater,
        initial_account_index=0,
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

