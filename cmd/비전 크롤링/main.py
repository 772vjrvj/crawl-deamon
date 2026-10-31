# -*- coding: utf-8 -*-
# PLACE-20260918-R3: 즉시 실행 / 빠른 스크롤 / 발견 즉시 종료 / 페이지 로그

import copy
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.utils.daemon_logger import setup_logger
from src.utils.selenium_utils import SeleniumUtils


PROJECT_ROOT = Path(__file__).resolve().parent

LOG_RETENTION_DAYS = 30

# 기존 보정 Python 설정값을 그대로 사용합니다.
SELECT_URL = "https://주식회사비전.com/user/place/rest/select-currentrank"
UPDATE_URL = "https://주식회사비전.com/user/place/rest/update-currentrank"
MAP_URL = "https://map.naver.com"

# 실행 설정
API_TIMEOUT = 30
PART_INDEX = 2
PART_COUNT = 3
RESTART_INTERVAL = 50
HEADLESS = False
SELENIUM_DEBUG = True
RUN_MODE = "NOW"  # "NOW": 즉시 1회 실행 후 종료, "SCHEDULE": 예약 실행
SCHEDULE_TIME = "02:30"
SCROLL_STEP = 900
SCROLL_DELAY = 0.1
PAGE_DELAY = 0.8
BOTTOM_WAIT = 0.5  # 바닥에서 추가 로딩이 없는지 확인하는 시간
PAGE_LOAD_TIMEOUT = 8  # 결과 목록과 페이지 전환을 기다리는 최대 시간


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_name(value: Any) -> str:
    return " ".join(
        str(value or "")
        .replace("\xa0", " ")
        .replace("\u200b", "")
        .split()
    )


def split_into_parts(data: List[Dict[str, Any]], part_count: int) -> List[List[Dict[str, Any]]]:
    if part_count <= 0:
        raise ValueError("PLACE_PART_COUNT는 1 이상이어야 합니다.")

    base_size = len(data) // part_count
    remainder = len(data) % part_count
    result: List[List[Dict[str, Any]]] = []
    start = 0

    for index in range(part_count):
        size = base_size + (1 if index < remainder else 0)
        result.append(data[start:start + size])
        start += size

    return result


def get_current_rank(
        session: requests.Session,
        logger: logging.Logger,
) -> List[Dict[str, Any]]:
    try:
        response = session.get(
            SELECT_URL,
            params={"type": "none"},
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            raise RuntimeError("현재 순위 API 응답이 배열이 아닙니다.")

        split_data = split_into_parts(data, PART_COUNT)
        selected = split_data[PART_INDEX]

        logger.info(
            "[PLACE] 현재 순위 조회 완료 | 전체=%s | 분할=%s | 대상=%s | 대상건수=%s",
            len(data),
            PART_COUNT,
            PART_INDEX,
            len(selected),
        )
        logger.info(
            "[PLACE] 시작 현재순위 | %s",
            [
                {
                    "keyword": item.get("keyword"),
                    "businessName": item.get("businessName"),
                    "placeNumber": item.get("placeNumber"),
                    "currentRank": item.get("currentRank"),
                }
                for item in selected
            ],
        )
        return selected

    except Exception as error:
        logger.error("[PLACE] 현재 순위 조회 실패: %s", error)
        return []


def update_object_list(
        session: requests.Session,
        objects: List[Dict[str, Any]],
        logger: logging.Logger,
) -> bool:
    if not objects:
        return True

    try:
        response = session.put(
            UPDATE_URL,
            json=objects,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        logger.info("[PLACE] 순위 업데이트 완료 | 건수=%s", len(objects))
        return True
    except Exception as error:
        logger.error("[PLACE] 순위 업데이트 실패 | 건수=%s | 오류=%s", len(objects), error)
        return False


def create_browser(logger: logging.Logger) -> Tuple[SeleniumUtils, Any]:
    selenium_utils = SeleniumUtils(
        headless=HEADLESS,
        debug=SELENIUM_DEBUG,
        log_func=lambda message: logger.info("%s", message),
    )
    selenium_utils.set_default_view_config(
        view_mode="browser",
        browser_window_size=(1000, 1000),
    )

    try:
        driver = selenium_utils.start_driver(
            timeout=30,
            view_mode="browser",
            window_size=(1000, 1000),
        )
        driver.get(MAP_URL)
        selenium_utils.wait_ready_state_complete(timeout_sec=10)
        time.sleep(2)
        return selenium_utils, driver
    except Exception:
        selenium_utils.quit()
        raise


def wait_for_iframe_and_switch(driver: Any, timeout: int = 10) -> bool:
    try:
        driver.switch_to.default_content()
        WebDriverWait(driver, timeout).until(
            EC.frame_to_be_available_and_switch_to_it((By.ID, "searchIframe"))
        )
        return True
    except Exception:
        return False


def input_search_keyword(driver: Any, keyword: str) -> None:
    driver.switch_to.default_content()
    search_input = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CLASS_NAME, "input_search"))
    )
    search_input.click()
    search_input.clear()
    search_input.send_keys(Keys.CONTROL + "a")
    search_input.send_keys(Keys.DELETE)
    time.sleep(0.3)
    search_input.send_keys(keyword)
    time.sleep(0.5)
    search_input.send_keys(Keys.ENTER)
    time.sleep(2.5)


def search_keyword_on_map(
        driver: Any,
        obj: Dict[str, Any],
        logger: logging.Logger,
) -> bool:
    keyword = str(obj.get("keyword") or "")

    try:
        input_search_keyword(driver, keyword)
        return True
    except Exception as error:
        logger.warning("[PLACE] 검색창 오류 | keyword=%s | 오류=%s", keyword, error)

        try:
            driver.get(MAP_URL)
            time.sleep(2)
            input_search_keyword(driver, keyword)
            return True
        except Exception as retry_error:
            logger.error(
                "[PLACE] 검색창 재시도 실패 | keyword=%s | 오류=%s",
                keyword,
                retry_error,
            )
            return False


def real_time_rank(driver, scrollable_div, business_names, target_name):
    try:
        # 업체마다 Selenium을 여러 번 호출하지 않고 현재 목록을 한 번에 읽습니다.
        names = driver.execute_script("""
            return Array.from(arguments[0].querySelectorAll("ul > li"))
                .filter(li => !Array.from(li.querySelectorAll("span.place_blind"))
                    .some(el => el.textContent.trim() === "광고"))
                .map(li => {
                    const link = li.querySelector('a[data-nlog-area$=".result"], .place_bluelink');
                    if (!link) return "";
                    const name = link.querySelector("span") || link;
                    return name.innerText || name.textContent || "";
                });
        """, scrollable_div)
        for name in names:
            business_name = normalize_name(name)
            if not business_name:
                continue
            if business_name not in business_names:
                business_names.append(business_name)
            if business_name == target_name:
                return business_names.index(business_name) + 1
        return -1
    except Exception:
        return None


def scroll_slowly_to_bottom(driver, obj, logger):
    target_name = normalize_name(obj.get("businessName"))
    if not target_name:
        logger.error("[PLACE] 상호명 없음 | keyword=%s", obj.get("keyword"))
        return None

    try:
        if not wait_for_iframe_and_switch(driver):
            logger.error("[PLACE] iframe 로딩 실패 | businessName=%s", target_name)
            return None

        business_names = []
        bottom_state = None
        bottom_since = 0.0
        page_selector = "div.zRM9F > a.mBN2s"
        page_needs_load = True

        while True:
            try:
                scrollable_div = WebDriverWait(driver, 4).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "div#_pcmap_list_scroll_container")
                    )
                )
            except TimeoutException:
                no_results = driver.find_elements(By.CLASS_NAME, "FYvSc")
                if any(p.text.strip() == "조건에 맞는 업체가 없습니다." for p in no_results):
                    return 999
                return None

            if page_needs_load:
                # 컨테이너만 생긴 상태가 아니라 업체명 요소가 실제 로딩될 때까지 기다립니다.
                WebDriverWait(driver, PAGE_LOAD_TIMEOUT, poll_frequency=0.1).until(
                    lambda d: d.execute_script(
                        """return !!arguments[0].querySelector(
                            'a[data-nlog-area$=".result"] > span, .place_bluelink span'
                        );""", scrollable_div,
                    )
                )
                page_needs_load = False

            # 매 스크롤 후 먼저 확인합니다. 바닥이어도 이 검사를 건너뛰지 않습니다.
            result = real_time_rank(driver, scrollable_div, business_names, target_name)
            if result is None:
                return None
            if result != -1:
                logger.info(
                    "[PLACE] 순위 발견 → 다음 대상 | keyword=%s | businessName=%s | rank=%s",
                    obj.get("keyword"), obj.get("businessName"), result,
                )
                return result

            top, height, view = driver.execute_script(
                "return [arguments[0].scrollTop, arguments[0].scrollHeight, "
                "arguments[0].clientHeight];", scrollable_div,
            )

            if top < height - view - 5:
                bottom_state = None
                driver.execute_script(
                    "arguments[0].scrollTop += arguments[1];", scrollable_div, SCROLL_STEP,
                )
                time.sleep(SCROLL_DELAY)
                continue

            # 마지막에 로딩된 업체도 확인한 뒤에만 페이지를 넘깁니다.
            state = (height, len(business_names))
            if bottom_state != state:
                bottom_state = state
                bottom_since = time.monotonic()
            if time.monotonic() - bottom_since < BOTTOM_WAIT:
                time.sleep(SCROLL_DELAY)
                continue

            if not business_names:
                logger.warning("[PLACE] 업체명 추출 실패 (미발견 999 처리 안 함) | keyword=%s", obj.get("keyword"))
                return None

            pages = driver.find_elements(By.CSS_SELECTOR, page_selector)
            current = next(
                (i for i, p in enumerate(pages) if "qxokY" in (p.get_attribute("class") or "")),
                -1,
            )
            if current == -1:
                logger.warning("[PLACE] 현재 페이지 확인 실패 | keyword=%s", obj.get("keyword"))
                return None

            old_page = normalize_name(pages[current].text)
            next_button = None
            next_page = ""
            if current + 1 < len(pages):
                next_button = pages[current + 1]
                next_page = normalize_name(next_button.text)
            else:
                # 숫자 묶음의 끝(예: 5페이지)도 '다음페이지'가 활성화돼 있으면 계속 탐색합니다.
                next_arrows = [
                    p for p in driver.find_elements(By.CSS_SELECTOR, "div.zRM9F > a")
                    if normalize_name(p.text) == "다음페이지"
                ]
                if not next_arrows:
                    logger.warning("[PLACE] 다음페이지 버튼 확인 실패 | keyword=%s", obj.get("keyword"))
                    return None
                if next_arrows[0].get_attribute("aria-disabled") != "true":
                    next_button = next_arrows[0]
                    next_page = str(int(old_page) + 1) if old_page.isdigit() else "다음"
            if next_button is None:
                logger.info(
                    "[PLACE] 마지막 페이지까지 대상 미발견 | keyword=%s | businessName=%s | 확인업체=%s",
                    obj.get("keyword"), obj.get("businessName"), len(business_names),
                )
                return 999

            logger.info(
                "[PLACE] 페이지 이동 요청 | keyword=%s | businessName=%s | page=%s -> %s | 누적업체=%s",
                obj.get("keyword"), obj.get("businessName"), old_page, next_page, len(business_names),
            )
            driver.execute_script("arguments[0].click();", next_button)
            time.sleep(PAGE_DELAY)
            WebDriverWait(driver, PAGE_LOAD_TIMEOUT, poll_frequency=0.1).until(
                lambda d: any(
                    "qxokY" in (p.get_attribute("class") or "")
                    and normalize_name(p.text) != old_page
                    for p in d.find_elements(By.CSS_SELECTOR, page_selector)
                )
            )
            bottom_state = None
            page_needs_load = True
            # 다음 반복에서도 같은 obj(키워드/상호명)를 계속 조사합니다.

    except Exception:
        logger.exception(
            "[PLACE] 순위 조회 실패 | keyword=%s | businessName=%s",
            obj.get("keyword"), obj.get("businessName"),
        )
        return None


def crawl_objects(
        objects: List[Dict[str, Any]],
        logger: logging.Logger,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    success_list: List[Dict[str, Any]] = []
    fail_list: List[Dict[str, Any]] = []
    selenium_utils: Optional[SeleniumUtils] = None
    driver: Any = None

    try:
        selenium_utils, driver = create_browser(logger)

        for index, obj in enumerate(objects, start=1):
            if index > 1 and (index - 1) % RESTART_INTERVAL == 0:
                selenium_utils.quit()
                selenium_utils, driver = create_browser(logger)
                logger.info("[PLACE] 드라이버 재시작 | index=%s", index)

            if str(obj.get("crawlYn") or "") == "N":
                continue

            logger.info(
                "[PLACE] 처리 시작 | %s/%s | keyword=%s | businessName=%s",
                index,
                len(objects),
                obj.get("keyword"),
                obj.get("businessName"),
            )

            if not search_keyword_on_map(driver, obj, logger):
                fail_list.append(obj)
                continue

            current_rank = scroll_slowly_to_bottom(driver, obj, logger)
            if current_rank is None:
                obj["crawlSuccessYn"] = "N"
                fail_list.append(obj)
                continue

            old_rank = int(obj.get("currentRank") or 0)
            highest_rank = int(obj.get("highestRank") or 999999)
            current_time = now_text()

            obj["recentRank"] = obj.get("currentRank")
            obj["rankChkDt"] = current_time

            if obj.get("correctYn") == "N" and old_rank != int(current_rank):
                obj["correctYn"] = "Y"
                obj["highestRank"] = current_rank
                obj["initialRank"] = current_rank
                obj["highestDt"] = current_time
            elif highest_rank > int(current_rank):
                obj["highestRank"] = current_rank
                obj["highestDt"] = current_time

            obj["currentRank"] = current_rank
            obj["crawlSuccessYn"] = "Y"
            success_list.append(obj)

            logger.info(
                "[PLACE] 처리 완료 | %s/%s | keyword=%s | businessName=%s | rank=%s",
                index,
                len(objects),
                obj.get("keyword"),
                obj.get("businessName"),
                current_rank,
            )

    finally:
        if selenium_utils is not None:
            selenium_utils.quit()

    return success_list, fail_list


def run_once(logger: logging.Logger) -> None:
    logger.info("[PLACE] 순위 보정 시작")

    with requests.Session() as session:
        objects = get_current_rank(session, logger)
        if not objects:
            logger.warning("[PLACE] 처리할 대상이 없습니다.")
            return

        success_list, fail_list = crawl_objects(objects, logger)
        update_object_list(session, success_list, logger)

        if fail_list:
            logger.info("[PLACE] 실패 대상 재처리 시작 | 건수=%s", len(fail_list))
            retry_success, retry_fail = crawl_objects(
                copy.deepcopy(fail_list),
                logger,
            )
            update_object_list(session, retry_success, logger)
            logger.info(
                "[PLACE] 재처리 완료 | 성공=%s | 실패=%s",
                len(retry_success),
                len(retry_fail),
            )

        logger.info(
            "[PLACE] 순위 보정 종료 | 최초성공=%s | 최초실패=%s",
            len(success_list),
            len(fail_list),
        )


def validate_config() -> None:
    if RUN_MODE not in ("NOW", "SCHEDULE"):
        raise RuntimeError('RUN_MODE는 "NOW" 또는 "SCHEDULE"이어야 합니다.')

    if not 0 <= PART_INDEX < PART_COUNT:
        raise RuntimeError(
            "PLACE_PART_INDEX는 PLACE_PART_COUNT보다 작아야 합니다."
        )

    try:
        datetime.strptime(SCHEDULE_TIME, "%H:%M")
    except ValueError as error:
        raise RuntimeError("PLACE_SCHEDULE_TIME 형식은 HH:MM이어야 합니다.") from error


def main() -> None:
    validate_config()
    logger = setup_logger(
        project_root=PROJECT_ROOT,
        retention_days=LOG_RETENTION_DAYS,
    )

    logger.info("[PLACE] Place 전용 데몬 시작 | version=PLACE-20260918-R3")
    logger.info("[PLACE] 실행모드=%s | 실행시간=%s | HEADLESS=%s", RUN_MODE, SCHEDULE_TIME, HEADLESS)

    if RUN_MODE == "NOW":
        run_once(logger)
        return

    try:
        last_run_date: Optional[date] = None
        while True:
            now = datetime.now()
            if (
                    now.strftime("%H:%M") == SCHEDULE_TIME
                    and last_run_date != now.date()
            ):
                run_once(logger)
                last_run_date = now.date()

            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("[PLACE] 사용자가 데몬을 종료했습니다.")
    except Exception:
        logger.exception("[PLACE] 치명적인 오류")
        raise


if __name__ == "__main__":
    main()
