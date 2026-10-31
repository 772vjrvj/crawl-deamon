# -*- coding: utf-8 -*-

import copy
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.action_chains import ActionChains
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
RUN_ON_START = True
SCHEDULE_TIME = "02:30"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
            params={"type": "one"},
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


def real_time_rank(
        scrollable_div: Any,
        business_names: List[str],
        target_name: str,
) -> Optional[int]:
    try:
        li_elements = scrollable_div.find_elements(By.CSS_SELECTOR, "ul > li")
    except (NoSuchElementException, StaleElementReferenceException):
        return None
    except Exception:
        return None

    for index, li in enumerate(li_elements):
        try:
            ad_elements = li.find_elements(By.CSS_SELECTOR, "span.place_blind")
            if any(ad.text.strip() == "광고" for ad in ad_elements):
                continue

            bluelink_div = li.find_element(By.CLASS_NAME, "place_bluelink")
            span_elements = bluelink_div.find_elements(By.TAG_NAME, "span")
            name_element = span_elements[0] if span_elements else None

            if name_element:
                business_name = name_element.text.strip()
                if business_name and business_name not in business_names:
                    business_names.append(business_name)

            if target_name in business_names:
                return business_names.index(target_name) + 1

        except Exception:
            continue

    return -1


def scroll_slowly_to_bottom(
        driver: Any,
        obj: Dict[str, Any],
        logger: logging.Logger,
) -> Optional[int]:
    try:
        driver.switch_to.default_content()

        if not wait_for_iframe_and_switch(driver):
            logger.error(
                "[PLACE] iframe 로딩 실패 | businessName=%s",
                obj.get("businessName", ""),
            )
            return None

        scrollable_div_selector = "div#_pcmap_list_scroll_container"
        target_name = str(obj.get("businessName") or "").strip()
        business_names: List[str] = []

        while True:
            try:
                scrollable_div = WebDriverWait(driver, 4).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, scrollable_div_selector)
                    )
                )
            except TimeoutException:
                try:
                    no_result_div = driver.find_element(By.CLASS_NAME, "FYvSc")
                    if no_result_div.text.strip() == "조건에 맞는 업체가 없습니다.":
                        return 999
                except Exception:
                    pass
                return None

            try:
                ActionChains(driver).move_to_element(scrollable_div).perform()
            except Exception:
                return None

            time.sleep(1)

            result = real_time_rank(
                scrollable_div,
                business_names,
                target_name,
            )
            if result is not None and result != -1:
                return result
            if result is None:
                return None

            try:
                for _ in range(7):
                    driver.execute_script(
                        "arguments[0].scrollTop += 250;",
                        scrollable_div,
                    )
                    time.sleep(0.2)
                time.sleep(1)

                current_scroll = driver.execute_script(
                    "return arguments[0].scrollTop;",
                    scrollable_div,
                )
                max_scroll_height = driver.execute_script(
                    "return arguments[0].scrollHeight - arguments[0].clientHeight;",
                    scrollable_div,
                )

                if current_scroll < max_scroll_height - 5:
                    result = real_time_rank(
                        scrollable_div,
                        business_names,
                        target_name,
                    )
                    if result is not None and result != -1:
                        return result
                    if result is None:
                        return None
                    continue

            except Exception as error:
                logger.warning("[PLACE] 스크롤 처리 실패 | 오류=%s", error)
                return None

            try:
                pages = driver.find_elements(By.CSS_SELECTOR, "div.zRM9F > a.mBN2s")
            except Exception:
                return None

            current_page_index = -1
            for index, page in enumerate(pages):
                try:
                    if "qxokY" in (page.get_attribute("class") or ""):
                        current_page_index = index
                        break
                except Exception:
                    continue

            if current_page_index + 1 >= len(pages):
                return len(business_names) + 1

            try:
                next_page_button = pages[current_page_index + 1]
                driver.execute_script("arguments[0].click();", next_page_button)
                time.sleep(2)
            except Exception:
                return None

    except Exception as error:
        logger.error(
            "[PLACE] 순위 조회 실패 | businessName=%s | 오류=%s",
            obj.get("businessName", ""),
            error,
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

    logger.info("[PLACE] Place 전용 데몬 시작")
    logger.info("[PLACE] 실행시간=%s | 시작즉시=%s | HEADLESS=%s", SCHEDULE_TIME, RUN_ON_START, HEADLESS)

    last_run_date: Optional[date] = None

    try:
        if RUN_ON_START:
            run_once(logger)
            last_run_date = datetime.now().date()

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
