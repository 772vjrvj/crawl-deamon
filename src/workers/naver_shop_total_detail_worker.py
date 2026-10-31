from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import pyautogui
import pyperclip

from src.database.db_connection import get_required_env
from src.database.execution_repository import claim_next_rank_item, save_rank_result
from src.workers.daemon_base_worker import (
    DaemonBaseWorker,
    ExistingStoreLoader,
    ResultInserter,
)

import os
import time
import winreg

from dotenv import load_dotenv
import pygetwindow as gw




# 순위 처리는 이 Worker의 안전 지점에서만 호출한다.
_rank_runner = None


def register_rank_runner(runner) -> None:
    global _rank_runner
    _rank_runner = runner


def clear_rank_runner() -> None:
    global _rank_runner
    _rank_runner = None


def run_one_rank(worker, context: str) -> bool:
    if _rank_runner is None or not worker.running:
        return False
    return bool(_rank_runner(worker, context))


class NaverShopTotalDetailWorker(DaemonBaseWorker):
    """네이버 쇼핑 상품과 스토어 방문자 수를 수집한다."""

    CAPTCHA_FAIL = 0
    CAPTCHA_NONE = 1
    CAPTCHA_SOLVED = 2
    ACCESS_LIMITED = 3
    SUCCESS = 5

    ACCESS_LIMIT_TEXTS: Tuple[str, ...] = (
        "쇼핑 서비스 접속이 일시적으로 제한되었습니다",
        "해당 네트워크의 접속을 일시적으로 제한",
        "비정상적인 접근이 감지",
    )


    def __init__(
            self,
            setting: Dict[str, Any],
            logger: Any,
            result_inserter: ResultInserter,
            existing_store_loader: ExistingStoreLoader,
    ) -> None:
        super().__init__(setting=setting, logger=logger)

        self.result_inserter = result_inserter
        self.existing_store_loader = existing_store_loader

        self.site_name: str = "naver_shop"
        self.worker_name: str = "naver_shop_total_detail"
        self.detail_table_name: str = "naver_shop_total_detail"
        self.detail_log_fields: Tuple[str, ...] = (
            "product_name",
            "store_name",
        )

        self.running: bool = True
        self.init_flag: bool = False
        self._cleaned_up: bool = False
        self._stop_event = threading.Event()
        self._browser_open: bool = False

        # main.py가 순위 기능을 켠 경우에만 설정한다.
        self.rank_connection_factory = None
        self.rank_user_service_id = 8

        self.total_cnt: int = 0
        self.current_cnt: int = 0
        self.before_pro_value: float = 0.0

        self.folder_path: str = ""
        self.out_dir: str = "output"
        self.auto_save_yn: bool = False

        self.dup_yn: bool = False
        self.seen_store_names: Set[str] = set()

        self.excel_driver: Optional[Any] = None
        self.db_repository: Optional[Any] = None
        self.columns: List[str] = []
        self.inserted_count: int = 0
        self.failed_count: int = 0

    def configure_rank(self, connection_factory, user_service_id: int = 8) -> None:
        self.rank_connection_factory = connection_factory
        self.rank_user_service_id = int(user_service_id)

    def _claim_rank_item(self):
        """USER_SERVICE_ID=8인 순위 요청에서 처리할 상세 1건을 가져온다."""
        if self.rank_connection_factory is None:
            return None
        connection = self.rank_connection_factory()
        try:
            return claim_next_rank_item(connection, self.rank_user_service_id)
        finally:
            connection.close()

    def _save_rank_result(self, claim, status, rank_no, result_code, result_data, error_message=None):
        connection = self.rank_connection_factory()
        try:
            save_rank_result(
                connection,
                claim,
                self.rank_user_service_id,
                status,
                rank_no,
                result_code,
                result_data,
                error_message,
            )
        finally:
            connection.close()

    def _read_rank_page(self, browser, keyword: str, page: int):
        url = (
            "https://msearch.shopping.naver.com/search/all?"
            f"query={quote(keyword)}&origQuery={quote(keyword)}&"
            f"pagingIndex={page}&pagingSize=40&productSet=total&"
            "sort=rel&viewType=list"
        )
        pyautogui.hotkey("ctrl", "l")
        pyperclip.copy(url)
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")
        if not browser._sleep_or_stop(4.0, "순위 페이지 이동 대기"):
            raise RuntimeError("순위 조회가 중단되었습니다.")

        captcha = browser.handle_captcha_with_retry()
        if captcha in (browser.CAPTCHA_FAIL, browser.ACCESS_LIMITED):
            raise RuntimeError("순위 페이지 캡차 또는 접속 제한")

        pyautogui.hotkey("ctrl", "u")
        if not browser._sleep_or_stop(2.0, "순위 소스 대기"):
            raise RuntimeError("순위 조회가 중단되었습니다.")
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "c")
        if not browser._sleep_or_stop(1.0, "순위 소스 복사 대기"):
            raise RuntimeError("순위 조회가 중단되었습니다.")
        source = pyperclip.paste()
        pyautogui.hotkey("ctrl", "w")
        if browser.is_access_limited(source):
            raise RuntimeError("순위 페이지 접속 제한")
        return browser.extract_items_from_html(source)

    def process_rank_once(self, browser_worker=None, context="대기") -> bool:
        """DB에서 순위 1건만 가져와 조회하고 결과를 저장한다."""
        claim = self._claim_rank_item()
        if claim is None:
            return False

        browser = browser_worker or self
        try:
            if browser is self and not self.init_flag and not self.init():
                raise RuntimeError("순위 Worker 초기화에 실패했습니다.")
            if not browser._browser_open and not browser._open_chrome():
                raise RuntimeError("순위 조회용 Chrome을 열지 못했습니다.")

            found = None
            checked = 0
            for page in range(1, 11):
                for value in self._read_rank_page(browser, str(claim["KEYWORD"]), page):
                    item = value.get("item", value) if isinstance(value, dict) else {}
                    checked += 1
                    if str(item.get("id") or "") == str(claim["MID"]):
                        found = int(item.get("rank"))
                        break
                if found is not None:
                    break

            result = {
                "keyword": str(claim["KEYWORD"]),
                "mid": str(claim["MID"]),
                "rankNo": found,
                "rankText": f"{found}위" if found is not None else "400위 초과",
                "resultCode": "FOUND" if found is not None else "OVER_400",
                "checkedCount": checked,
                "checkedPages": page,
            }
            self._save_rank_result(
                claim, "SUCCESS", found,
                result["resultCode"], result,
            )
            self.log_signal_func(
                f"✅ 순위 완료 | 키워드={claim['KEYWORD']} | "
                f"MID={claim['MID']} | {result['rankText']}"
            )
        except Exception as error:
            result = {
                "keyword": str(claim["KEYWORD"]),
                "mid": str(claim["MID"]),
                "rankNo": None,
                "rankText": "조회 실패",
                "resultCode": "ERROR",
            }
            self._save_rank_result(claim, "FAIL", None, "ERROR", result, str(error))
            self.log_signal_func(f"❌ 순위 실패 | ITEM_ID={claim['ITEM_ID']} | {error}")
        return True

    # =========================================================
    # 초기화
    # =========================================================
    def init(self) -> bool:
        try:
            if self.init_flag:
                self.log_signal_func("이미 초기화가 완료되었습니다.")
                return True

            self.running = True
            self._cleaned_up = False
            self._stop_event.clear()
            self._browser_open = False

            # 콘솔 출력 대상이 없으면, 출력 내용을 운영체제의 휴지통으로 보내서 print() 관련 오류가 발생하지 않도록 한다.
            if sys.stdout is None:
                sys.stdout = open(os.devnull, "w")

            # 모든 자동 조작 명령 사이에 기본적으로 0.4초씩 대기하도록 설정합니다.
            pyautogui.PAUSE = 0.4

            # 마우스를 화면 왼쪽 위 모서리로 이동하면 pyautogui 자동화가 즉시 중단되도록 설정
            pyautogui.FAILSAFE = True

            self.init_flag = True
            self.log_signal_func("✅ init 완료")
            return True

        except Exception as e:
            self.log_signal_func(f"❌ 초기화 에러: {e}")
            return False


    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {
            "true",
            "1",
            "y",
            "yes",
            "on",
        }

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    # =========================================================
    # DB Repository
    # =========================================================
    def db_set(self) -> bool:
        # 서버 MariaDB 연결과 결과 저장은 공통 데몬이 담당한다.
        return True

    def finish_job(
            self,
            status: str,
            error_message: Optional[str] = None,
    ) -> None:
        # 실행 이력 최종 상태는 공통 main.py가 처리한다.
        return

    def insert_detail_row(
            self,
            row: Dict[str, Any],
            *,
            row_status: str = "SUCCESS",
            row_error_message: Optional[str] = None,
            row_start_at: Optional[str] = None,
            row_end_at: Optional[str] = None,
    ) -> bool:
        result_data = dict(row)
        result_data["row_start_at"] = row_start_at
        result_data["row_end_at"] = row_end_at

        try:
            self.result_inserter(
                row_status,
                result_data,
                row_error_message,
            )
            self.inserted_count += 1
            return True
        except Exception as error:
            self.failed_count += 1
            self.log_signal_func(f"❌ [DB] 상세 결과 저장 실패: {error}")
            raise

    def insert_detail_rows(
            self,
            rows: Sequence[Dict[str, Any]],
            *,
            row_status: str = "SUCCESS",
    ) -> bool:
        for row in rows:
            if not self.insert_detail_row(dict(row), row_status=row_status):
                return False
        return True

    @staticmethod
    def _now_db() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def load_existing_store_names(self) -> Set[str]:
        """
        같은 USER_SERVICE_ID의 과거 SUCCESS 결과에서 store_name을 읽는다.

        MariaDB 조회와 재연결은 공통 ExecutionResultStore가 담당한다.
        """
        result = self.existing_store_loader()

        self.log_signal_func(
            f"✅ [중복] DB 스토어명 {len(result)}건 로드 완료"
        )
        return result

    # =========================================================
    # Excel / 작업 마감
    # =========================================================
    def export_detail_to_excel(self) -> bool:
        # 데몬 버전에서는 자동 엑셀 저장을 사용하지 않는다.
        return False

    def finalize_db_and_excel(self) -> None:
        return

    # =========================================================
    # 종료 / 정리
    # =========================================================
    def stop(self) -> None:
        self.log_signal_func("✅ stop 시작")
        self.running = False
        self._stop_event.set()

        if self.db_repository and self.db_repository.status == "RUNNING":
            self.finish_job("STOP", "사용자 중단")

        self._close_browser()
        time.sleep(1)
        self.cleanup()
        self.log_signal_func("✅ stop 완료")

    def destroy(self) -> None:
        self.cleanup()
        self.progress_signal.emit(self.before_pro_value, 1000000)
        self.log_signal_func("✅ destroy")
        time.sleep(2.5)
        self.progress_end_signal.emit()

    def cleanup(self) -> None:
        if self._cleaned_up:
            return

        self._close_browser()

        # Repository 연결 종료 전에 hist 마감 및 엑셀 저장
        self.finalize_db_and_excel()

        try:
            if self.db_repository:
                self.db_repository.close()
        except Exception as e:
            self.log_signal_func(f"[cleanup] db_repository.close 실패: {e}")
        finally:
            self.db_repository = None

        try:
            if self.excel_driver:
                self.excel_driver.close()
        except Exception as e:
            self.log_signal_func(f"[cleanup] excel_driver.close 실패: {e}")
        finally:
            self.excel_driver = None

        self._cleaned_up = True

    # =========================================================
    # 브라우저 / 접속 제한
    # =========================================================
    def _open_chrome(self) -> bool:
        if not self.running or self._stop_event.is_set():
            return False

        pyautogui.hotkey("win", "r")
        if not self._sleep_or_stop(
                0.5,
                "🛑 중단 또는 sleep 실패 | chrome 실행 전 대기",
        ):
            return False

        # 한영키에서 한글로 되어 있으면 chrome이 한글로 나오는 이슈가 있어서 클립보드에 붙여서 넣음
        pyperclip.copy("chrome --new-window --start-maximized about:blank")
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")
        if not self._sleep_or_stop(
                3,
                "🛑 중단 또는 sleep 실패 | chrome 실행 후 대기",
        ):
            return False

        self._browser_open = True
        return True

    def _close_browser(self) -> None:
        if not self._browser_open:
            return

        try:
            pyautogui.hotkey("alt", "f4")
        except Exception:
            pass
        finally:
            self._browser_open = False

    def is_access_limited(self, page_content: str) -> bool:
        text = str(page_content or "")
        return any(limit_text in text for limit_text in self.ACCESS_LIMIT_TEXTS)

    def _reopen_list_page(self, target_url: str, context: str) -> bool:
        self.log_signal_func(
            f"🔄 브라우저 탭을 모두 닫고 목록을 다시 엽니다. | {context}"
        )
        self._close_browser()

        if not self._sleep_or_stop(
                1,
                "🛑 중단 또는 sleep 실패 | 브라우저 재실행 전 대기",
        ):
            return False

        if not self._open_chrome():
            return False

        pyautogui.hotkey("ctrl", "l")
        pyperclip.copy(target_url)
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")

        return self._sleep_or_stop(
            random.uniform(4.0, 5.5),
            f"🛑 중단 또는 sleep 실패 | 목록 다시 열기 후 대기 | {context}",
        )

    # =========================================================
    # 중복 제거
    # =========================================================
    def filter_chunk_items(
            self,
            chunk_items_queue: List[Dict[str, Any]],
            kw: str,
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []

        for item_data in chunk_items_queue:
            item = item_data.get("item", {})
            mall_name = str(item.get("mallName") or "").strip()

            if not mall_name:
                self.log_signal_func("⏭️ 스토어명 없음 스킵")
                continue

            if self.dup_yn and mall_name in self.seen_store_names:
                self.log_signal_func(f"⏭️ 스토어명 중복 스킵: {mall_name}")
                continue

            if self.dup_yn:
                # 현재 실행에서 동일 스토어 상세 요청을 반복하지 않는다.
                self.seen_store_names.add(mall_name)

            result.append(item_data)

        if self.dup_yn:
            self.log_signal_func(
                f"🧮 상세 대상 정리 완료 "
                f"| 원본={len(chunk_items_queue)} "
                f"| 중복제거후={len(result)} "
                f"| 키워드={kw}"
            )
        else:
            self.log_signal_func(
                f"🧮 상세 대상 정리 완료 "
                f"| 원본={len(chunk_items_queue)} "
                f"| 대상={len(result)} "
                f"| 키워드={kw}"
            )

        return result

    # =========================================================
    # 공통 보조 함수
    # =========================================================
    def _log_and_return_true(self, message: str) -> bool:
        self.log_signal_func(message)

        if self.db_repository and self.db_repository.status == "RUNNING":
            if self.running:
                self.finish_job("FAIL", message)
            else:
                self.finish_job("STOP", "사용자 중단")

        return True

    def _sleep_or_stop(self, sec: float, message: str) -> bool:
        if not self.sleep_s(sec):
            self.log_signal_func(message)
            return False
        return True


    def open_new_tab(self, url: str) -> None:
        """새 탭을 열고 지정한 URL로 이동한다."""
        pyautogui.hotkey("ctrl", "t")
        time.sleep(0.5)
        self.move_to_url(url)


    def move_to_url(self, url: str) -> None:
        """현재 탭에서 지정한 URL로 이동한다."""
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.2)

        pyperclip.copy(url)
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")

    def move_to_tab(self, num: int) -> None:
        """지정한 번호의 크롬 탭으로 이동한다."""
        if not 1 <= num <= 8:
            raise ValueError("탭 번호는 1부터 8까지 입력해야 합니다.")

        pyautogui.hotkey("ctrl", str(num))


    def move_mouse_to_window_center(self):
        """현재 활성화된 창의 정중앙으로 마우스를 이동한다."""
        window = gw.getActiveWindow()

        if window is None:
            raise RuntimeError("활성화된 창을 찾지 못했습니다.")

        center_x = window.left + window.width // 2
        center_y = window.top + window.height // 2

        pyautogui.moveTo(center_x, center_y, duration=0.5)

        print(f"마우스 이동 완료: x={center_x}, y={center_y}")
    
    
    def get_desktop_path(self) -> str:
        """Windows에 설정된 실제 바탕화면 경로를 가져온다."""
        registry_path = (
            r"Software\Microsoft\Windows\CurrentVersion"
            r"\Explorer\Shell Folders"
        )
    
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
            desktop_path, _ = winreg.QueryValueEx(key, "Desktop")
    
        return desktop_path


    def save_image_to_desktop(self):
        """마우스 위치의 이미지를 우클릭 메뉴를 통해 바탕화면에 저장한다."""
        save_path = os.path.join(self.get_desktop_path(), "test_test_test.png")
    
        # 이미지 위에서 우클릭
        pyautogui.rightClick()
        time.sleep(0.7)
    
        # 우클릭 메뉴의 두 번째 항목 선택
        pyautogui.press("down", presses=2, interval=0.2)
        pyautogui.press("enter")
    
        # 다른 이름으로 저장 창 대기
        time.sleep(2)
    
        # 파일 이름에 바탕화면 전체 경로 입력
        pyperclip.copy(save_path)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        pyautogui.press("enter")
    
        # 저장 처리 대기
        time.sleep(3)
    
        # 덮어쓰기 확인 창에서 왼쪽으로 이동 후 엔터
        pyautogui.press("left")
        pyautogui.press("enter")
    
        # 덮어쓰기 확인 창 대기 후 '예' 선택
        # pyautogui.press("y")

    
    def copy_content(self) -> str:
    
        # 현재 마우스 위치에서 왼쪽 클릭
        pyautogui.click()
    
        # 네이버 페이지 전체 선택
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.3)
    
        # 이전 클립보드 내용 비우기
        pyperclip.copy("")
    
        # 선택한 텍스트 복사
        pyautogui.hotkey("ctrl", "c")
    
        time.sleep(0.3)
    
        # 클립보드에 텍스트가 복사될 때까지 최대 3초 대기
        copied_text = ""
        deadline = time.monotonic() + 3
    
        while time.monotonic() < deadline:
            copied_text = pyperclip.paste()
    
            if copied_text:
                break
    
            time.sleep(0.1)
    
        if not copied_text:
            raise RuntimeError("네이버 페이지의 텍스트를 복사하지 못했습니다.")
    
        lines = copied_text.splitlines()
    
        # '아닙니다.'가 포함된 줄 찾기
        start_index = next(
            (i for i, line in enumerate(lines) if "아닙니다." in line),
            None,
        )
    
        if start_index is None:
            raise ValueError("'아닙니다.'가 포함된 문장을 찾지 못했습니다.")
    
        # 그다음 '입력해주세요.'가 포함된 줄 찾기
        end_index = next(
            (
                i
                for i in range(start_index + 1, len(lines))
                if "입력해주세요." in lines[i]
            ),
            None,
        )
    
        if end_index is None:
            raise ValueError("'입력해주세요.'가 포함된 문장을 찾지 못했습니다.")
    
        # 두 안내 문장 사이의 내용만 추출
        content = "\n".join(lines[start_index + 1:end_index]).strip()
    
        if not content:
            raise ValueError("두 안내 문장 사이에 내용이 없습니다.")
    
        # 추출한 내용만 클립보드에 저장
        pyperclip.copy(content)
    
        return content

    
    def upload_image_and_paste_content(self, content: str) -> None:
        # 바탕화면의 test.png 경로
        image_path = os.path.join(self.get_desktop_path(), "test_test_test.png")
    
        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"이미지 파일을 찾을 수 없습니다: {image_path}")
    
        # 업로드 버튼으로 이동 후 파일 선택 창 열기
        pyautogui.press("tab")
        time.sleep(1)
        pyautogui.press("enter")
        time.sleep(1)
        pyautogui.press("enter")
    
        # 파일 선택 창이 열릴 때까지 대기
        time.sleep(2)
    
        # 이미지 경로 입력 후 선택
        pyperclip.copy(image_path)
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")
    
        # 이미지 업로드 및 입력창 포커스 이동 대기
        time.sleep(3)
    
        # 내용 붙여넣기
        pyperclip.copy(content)
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")
    
        time.sleep(18)
    
    
        # Shift + Tab 두 번
        pyautogui.hotkey("shift", "tab")
        time.sleep(1)
        pyautogui.hotkey("shift", "tab")
        time.sleep(1)
    
        # Tab 한 번
        pyautogui.press("tab")
        time.sleep(1)
    
        # Enter
        pyautogui.press("enter")
        time.sleep(2)
    
        pyautogui.hotkey("tab")
        pyautogui.hotkey("tab")
        pyautogui.hotkey("tab")
        pyautogui.hotkey("tab")
        pyautogui.hotkey("tab")
        pyautogui.hotkey("tab")




    # =========================================================
    # main
    # =========================================================
    def main(self) -> bool:
        try:
            return self._main_impl()
        except Exception as e:
            error_message = str(e)
            self.log_signal_func(f"❌ 전체 실행 중 예외 발생: {error_message}")
            self.finish_job("FAIL", error_message)
            self._close_browser()
            return False

    def _main_impl(self) -> bool:
        keywords_str = str(
            self.get_setting_value(self.setting, "keyword") or ""
        )

        # 고객이 동일 키워드를 여러 바퀴 실행하려는 현재 운영 방식은 유지한다.
        # 따라서 콤마 분리만 하고 키워드 중복은 제거하지 않는다.
        keywords = [
            keyword.strip()
            for keyword in keywords_str.split(",")
            if keyword.strip()
        ]

        # 시작 페이지 설정
        start_p = self._to_int(
            self.get_setting_value(self.setting, "start_page"),
            1,
        )

        # 종료 페이지 설정
        end_p = self._to_int(
            self.get_setting_value(self.setting, "end_page"),
            start_p,
        )

        # 기준 방문자 수(같거나 작은 경우 추출)
        site_total_cnt = self._to_int(
            self.get_setting_value(self.setting, "site_total_cnt"),
            0,
        )

        # 엑셀 저장 경로
        self.folder_path = str(
            self.get_setting_value(self.setting, "folder_path") or ""
        ).strip()

        # 중복제거
        self.dup_yn = self._to_bool(
            self.get_setting_value(self.setting, "dup_yn")
        )

        # DB에서 이전 상점이름을 가져와서 중복에서 제거set에 넣는다.
        self.seen_store_names = (
            self.load_existing_store_names()
            if self.dup_yn
            else set()
        )

        if self.dup_yn:
            self.log_signal_func(
                f"✅ [중복] 스토어명 중복제거 사용 "
                f"| 초기 DB 건수={len(self.seen_store_names)}"
            )
        else:
            self.log_signal_func("ℹ️ [중복] 스토어명 중복제거 미사용")

        if not keywords:
            self.log_signal_func("❌ 키워드가 없습니다.")
            self.finish_job("FAIL", "키워드가 없습니다.")
            return False

        if start_p < 1:
            start_p = 1

        if end_p < start_p:
            error_message = "종료페이지가 시작페이지보다 작습니다."
            self.log_signal_func(f"❌ {error_message}")
            self.finish_job("FAIL", error_message)
            return False

        total_pages = end_p - start_p + 1
        self.total_cnt = len(keywords) * total_pages
        self.current_cnt = 0
        self.before_pro_value = 0.0

        self.log_signal_func(
            f"🚀 작업 시작 "
            f"| 키워드수={len(keywords)} "
            f"| 페이지={start_p}~{end_p} "
            f"| 총 작업단위={self.total_cnt}"
        )

        completed_keywords = 0

        for kw_idx, kw in enumerate(keywords, start=1):

            if not self.running:
                return self._log_and_return_true(
                    f"🛑 사용자 중단 감지(main-키워드 시작 전) "
                    f"| 마지막완료키워드수={completed_keywords} "
                    f"| 다음키워드={kw}"
                )

            keyword_start_cnt = self.current_cnt
            self.log_signal_func(
                f"🔎 [키워드 시작 {kw_idx}/{len(keywords)}] "
                f"{kw} | page={start_p}~{end_p}"
            )

            all_pages = list(range(start_p, end_p + 1))

            # chunk_size = 10
            chunk_size = 3

            for i in range(0, len(all_pages), chunk_size):

                if not self.running:
                    return self._log_and_return_true(
                        f"🛑 사용자 중단 감지(chunk 시작 전) "
                        f"| 키워드={kw} | chunk_index={i}"
                    )

                current_chunk = all_pages[i: i + chunk_size]
                chunk_items_queue: List[Dict[str, Any]] = []

                self.log_signal_func(
                    f"🌐 [브라우저 시작] 키워드={kw} "
                    f"| chunk={current_chunk[0]}p~{current_chunk[-1]}p"
                )

                if not self._open_chrome():
                    return self._log_and_return_true(
                        f"🛑 Chrome 실행 실패 또는 사용자 중단 | 키워드={kw}"
                    )

                target_url_gpt = get_required_env("CHAT_GPT_ANSWER")

                # gpt 열기
                self.open_new_tab(target_url_gpt)
                time.sleep(2)

                # 5. 첫 번째 탭으로 이동
                self.move_to_tab(1)
                time.sleep(1)


                self.log_signal_func(
                    f"📂 [{kw}] {current_chunk[0]}p ~ "
                    f"{current_chunk[-1]}p 리스트 확보 중..."
                )

                for page in current_chunk:
                    if not self.running:
                        return self._log_and_return_true(
                            f"🛑 사용자 중단 감지(페이지 시작 전) "
                            f"| 키워드={kw} | page={page}"
                        )

                    target_url = (
                        "https://msearch.shopping.naver.com/search/all?"
                        f"adQuery={kw}&npayType=2&origQuery={kw}&"
                        f"pagingIndex={page}&pagingSize=40&productSet=checkout&"
                        f"query={kw}&sort=date&viewType=list"
                    )

                    page_success = False
                    retry = 1

                    # 접속 제한으로 브라우저를 다시 연 경우,
                    # 다음 반복에서 동일 URL을 다시 입력하지 않도록 한다.
                    skip_page_navigation = False

                    # 접속 제한은 일반 추출 재시도 횟수에 포함하지 않는다.
                    while retry <= 3:
                        if not self.running:
                            return self._log_and_return_true(
                                f"🛑 사용자 중단 감지(리스트 재시도 중) "
                                f"| 키워드={kw} | page={page} | retry={retry}"
                            )

                        self.log_signal_func(
                            f"📄 [리스트 페이지 시도] "
                            f"키워드={kw} | page={page} | retry={retry}/3"
                        )

                        # _reopen_list_page()에서 이미 target_url을 열었다면
                        # 동일 URL을 다시 요청하지 않는다.
                        if skip_page_navigation:
                            skip_page_navigation = False

                        else:
                            pyautogui.hotkey("ctrl", "l")
                            if not self._sleep_or_stop(
                                    random.uniform(0.2, 0.5),
                                    "🛑 중단 또는 sleep 실패 | 주소창 이동 후 대기 "
                                    f"| 키워드={kw} | page={page} | retry={retry}",
                            ):
                                return True

                            # 네이버 샵 열기
                            pyperclip.copy(target_url)
                            pyautogui.hotkey("ctrl", "v")
                            pyautogui.press("enter")

                            if not self._sleep_or_stop(
                                    random.uniform(4.0, 5.5),
                                    "🛑 중단 또는 sleep 실패 | 페이지 이동 후 대기 "
                                    f"| 키워드={kw} | page={page} | retry={retry}",
                            ):
                                return True

                        captcha_result = self.handle_captcha_with_retry()

                        if captcha_result == self.ACCESS_LIMITED:
                            if not self._reopen_list_page(
                                    target_url,
                                    f"목록 | 키워드={kw} | page={page}",
                            ):
                                return not self.running

                            # _reopen_list_page()가 이미 target_url까지 접속했으므로
                            # 다음 반복에서는 주소 이동을 생략한다.
                            skip_page_navigation = True
                            continue

                        if captcha_result == self.CAPTCHA_FAIL:
                            if not self.running:
                                return self._log_and_return_true(
                                    "🛑 사용자 중단 감지(리스트 캡차 처리 중) "
                                    f"| 키워드={kw} | page={page}"
                                )

                            error_message = (
                                "리스트 캡차 실패 "
                                f"| 키워드={kw} | page={page}"
                            )
                            self.log_signal_func(f"❌ {error_message}")
                            self._close_browser()
                            self.finish_job("FAIL", error_message)
                            return False

                        pyautogui.hotkey("ctrl", "u")
                        if not self._sleep_or_stop(
                                random.uniform(3.0, 4.0),
                                "🛑 중단 또는 sleep 실패 | 소스보기 대기 "
                                f"| 키워드={kw} | page={page} | retry={retry}",
                        ):
                            return True

                        pyautogui.hotkey("ctrl", "a")
                        pyautogui.hotkey("ctrl", "c")
                        if not self._sleep_or_stop(
                                1.5,
                                "🛑 중단 또는 sleep 실패 | HTML 복사 대기 "
                                f"| 키워드={kw} | page={page} | retry={retry}",
                        ):
                            return True

                        html_source = pyperclip.paste()
                        pyautogui.hotkey("ctrl", "w")

                        # 소스에서 접속 제한을 발견한 경우도 동일하게 처리한다.
                        if self.is_access_limited(html_source):
                            if not self._reopen_list_page(
                                    target_url,
                                    f"목록 소스 | 키워드={kw} | page={page}",
                            ):
                                return not self.running

                            skip_page_navigation = True
                            continue

                        extracted = self.extract_items_from_html(html_source)

                        if extracted:
                            for item in extracted:
                                item["_page_num"] = page
                                item["_list_url"] = target_url

                            chunk_items_queue.extend(extracted)

                            self.log_signal_func(
                                f"📄 {page}페이지 수집 완료: "
                                f"상품 {len(extracted)}개 확보 "
                                f"| 키워드={kw} | retry={retry}"
                            )

                            page_success = True
                            break

                        self.log_signal_func(
                            f"⚠️ 리스트 추출 실패 "
                            f"| 키워드={kw} | page={page} | retry={retry}/3"
                        )

                        retry += 1

                        if retry <= 3:
                            if not self._sleep_or_stop(
                                    random.uniform(2.0, 3.5),
                                    "🛑 중단 또는 sleep 실패 | 리스트 재시도 전 대기 "
                                    f"| 키워드={kw} | page={page} "
                                    f"| retry={retry - 1}",
                            ):
                                return True

                    if not page_success:
                        self.log_signal_func(
                            f"❌ 페이지 최종 실패 "
                            f"| 키워드={kw} | page={page} "
                            "| 3회 재시도 후 추출 실패"
                        )

                    # 접속 제한 대기 중에는 이 위치까지 오지 않으므로 진행률이 증가하지 않는다.
                    self.current_cnt += 1

                    run_one_rank(self, f"목록 완료 | 키워드={kw} | page={page}")

                if chunk_items_queue:
                    self.log_signal_func(
                        f"🚀 확보된 {len(chunk_items_queue)}개 상품 "
                        "상세 수집 시작..."
                    )

                    chunk_items_queue = self.filter_chunk_items(
                        chunk_items_queue,
                        kw,
                    )

                    for idx, item_data in enumerate(chunk_items_queue):
                        if not self.running:
                            return self._log_and_return_true(
                                f"🛑 사용자 중단 감지(상세 수집 중) "
                                f"| 키워드={kw} "
                                f"| 상세순번={idx + 1}/{len(chunk_items_queue)}"
                            )

                        item = item_data.get("item", {})
                        pc_url = str(item.get("mallPcUrl") or "").strip()
                        p_num = item_data.get("_page_num")
                        list_url = str(item_data.get("_list_url") or "").strip()

                        if not pc_url:
                            self.log_signal_func(
                                f"⚠️ 상세 URL 없음 스킵 "
                                f"| 키워드={kw} | page={p_num} | idx={idx + 1}"
                            )
                            run_one_rank(self, f"상세 URL 없음 | 키워드={kw} | idx={idx + 1}")
                            continue

                        row_start_at = self._now_db()

                        # 접속 제한이 발생하면 동일 상품 URL부터 다시 시작한다.
                        while True:
                            if not self.running:
                                return self._log_and_return_true(
                                    f"🛑 사용자 중단 감지(상세 재시도 중) "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}"
                                )

                            self.log_signal_func(
                                f"🔗 [{kw}] {p_num}p - "
                                f"{idx + 1}/{len(chunk_items_queue)} 상세 이동"
                            )

                            pyautogui.hotkey("ctrl", "l")
                            pyperclip.copy(pc_url)
                            pyautogui.hotkey("ctrl", "v")
                            pyautogui.press("enter")
                            if not self._sleep_or_stop(
                                    random.uniform(3.5, 5.0),
                                    "🛑 중단 또는 sleep 실패 | 상세 이동 후 대기 "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}",
                            ):
                                return True

                            pyautogui.scroll(random.randint(-600, -300))
                            if not self._sleep_or_stop(
                                    random.uniform(0.5, 1.0),
                                    "🛑 중단 또는 sleep 실패 | 상세 스크롤 후 대기 "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}",
                            ):
                                return True

                            pyautogui.scroll(random.randint(300, 600))

                            captcha_result = self.handle_captcha_with_retry()

                            if captcha_result == self.ACCESS_LIMITED:
                                if not self._reopen_list_page(
                                        list_url,
                                        f"상세 | 키워드={kw} | page={p_num} "
                                        f"| idx={idx + 1}",
                                ):
                                    return not self.running
                                continue

                            if captcha_result == self.CAPTCHA_SOLVED:
                                self.log_signal_func(
                                    f"🔄 상세 캡차 해결 후 재진입 "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}"
                                )
                                pyautogui.hotkey("ctrl", "l")
                                pyperclip.copy(pc_url)
                                pyautogui.hotkey("ctrl", "v")
                                pyautogui.press("enter")
                                if not self._sleep_or_stop(
                                        random.uniform(3.0, 4.5),
                                        "🛑 중단 또는 sleep 실패 | 상세 재진입 후 대기 "
                                        f"| 키워드={kw} | page={p_num} | idx={idx + 1}",
                                ):
                                    return True

                            elif captcha_result == self.CAPTCHA_FAIL:
                                if not self.running:
                                    return self._log_and_return_true(
                                        "🛑 사용자 중단 감지(상세 캡차 처리 중) "
                                        f"| 키워드={kw} | page={p_num} "
                                        f"| idx={idx + 1}"
                                    )

                                error_message = (
                                    "상세 캡차 실패 "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}"
                                )
                                self.log_signal_func(f"❌ {error_message}")
                                self._close_browser()
                                self.finish_job("FAIL", error_message)
                                return False

                            pyautogui.hotkey("ctrl", "a")
                            if not self._sleep_or_stop(
                                    random.uniform(0.8, 1.2),
                                    "🛑 중단 또는 sleep 실패 | 상세 전체선택 후 대기 "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}",
                            ):
                                return True

                            pyautogui.hotkey("ctrl", "c")
                            if not self._sleep_or_stop(
                                    0.6,
                                    "🛑 중단 또는 sleep 실패 | 상세 복사 후 대기 "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}",
                            ):
                                return True

                            detail_text = pyperclip.paste()

                            if self.is_access_limited(detail_text):
                                if not self._reopen_list_page(
                                        list_url,
                                        f"상세 본문 | 키워드={kw} | page={p_num} "
                                        f"| idx={idx + 1}",
                                ):
                                    return not self.running
                                continue

                            break

                        total_visit = "0"
                        visit_match = re.search(
                            r"전체\s*([\d,]+)",
                            detail_text,
                        )
                        if visit_match:
                            total_visit = visit_match.group(1).replace(",", "")

                        if int(total_visit or 0) <= 0:
                            self.log_signal_func(
                                f"⏭️ 전체방문자수 0 스킵 "
                                f"| 키워드={kw} | page={p_num} "
                                f"| 스토어={item.get('mallName')}"
                            )
                            run_one_rank(self, f"방문자 0 스킵 | 키워드={kw} | idx={idx + 1}")
                            continue

                        categories = [
                            item.get("category1Name"),
                            item.get("category2Name"),
                            item.get("category3Name"),
                            item.get("category4Name"),
                        ]
                        category_str = "/".join(
                            str(category).strip()
                            for category in categories
                            if str(category or "").strip()
                        )

                        # Repository는 config.json columns[].code 기준 dict를 저장한다.
                        row = {
                            "keyword": kw,
                            "crawled_at": datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "product_name": item.get("productName"),
                            "category": category_str,
                            "product_no": item.get("id"),
                            "list_price": item.get("listPrice"),
                            "low_price": item.get("lowPrice"),
                            "sale_price": item.get("price"),
                            "delivery_fee": item.get("dlvryFee"),
                            "discount_ratio": item.get("discountRatio"),
                            "brand": item.get("brand"),
                            "review_count": item.get("reviewCount"),
                            "purchase_count": item.get("purchaseCnt"),
                            "wish_count": item.get("keepCnt"),
                            "store_name": item.get("mallName"),
                            "mall_prod_mbl_url": item.get("mallProdMblUrl"),
                            "mall_product_url": item.get("mallProductUrl"),
                            "pc_url": pc_url,
                            "total_visit_count": total_visit,
                            "page": p_num,
                            "no": idx + 1,
                        }

                        if site_total_cnt >= int(total_visit):
                            if not self.running:
                                return self._log_and_return_true(
                                    f"🛑 사용자 중단 감지(DB 저장 직전) "
                                    f"| 키워드={kw} | page={p_num} | idx={idx + 1}"
                                )

                            if not self.insert_detail_row(
                                    row,
                                    row_status="SUCCESS",
                                    row_start_at=row_start_at,
                                    row_end_at=self._now_db(),
                            ):
                                self.log_signal_func(
                                    f"❌ DB 저장 실패 "
                                    f"| 키워드={kw} | page={p_num} "
                                    f"| 방문자={total_visit} "
                                    f"| 기준={site_total_cnt}"
                                )

                        self.log_signal_func(
                            f"📦 [수집 완료] {kw} - {p_num}p "
                            f"| {item.get('mallName')} "
                            f"| 방문자: {total_visit}"
                        )

                        if not self._sleep_or_stop(
                                random.uniform(1.0, 2.5),
                                "🛑 중단 또는 sleep 실패 | 상세 간 대기 "
                                f"| 키워드={kw} | page={p_num} | idx={idx + 1}",
                        ):
                            return True

                        run_one_rank(self, f"상세 완료 | 키워드={kw} | idx={idx + 1}")

                else:
                    self.log_signal_func(
                        f"⚠️ chunk_items_queue 비어있음 "
                        f"| 키워드={kw} "
                        f"| chunk={current_chunk[0]}p~{current_chunk[-1]}p"
                    )

                self.log_signal_func(
                    "🧹 묶음 작업 완료. 브라우저를 정리합니다."
                )
                self._close_browser()
                if not self._sleep_or_stop(
                        2,
                        "🛑 중단 또는 sleep 실패 | 브라우저 종료 후 대기 "
                        f"| 키워드={kw} "
                        f"| chunk={current_chunk[0]}~{current_chunk[-1]}",
                ):
                    return True

                pro_value = (self.current_cnt / self.total_cnt) * 1000000
                self.progress_signal.emit(self.before_pro_value, pro_value)
                self.before_pro_value = pro_value
                self.log_signal_func(
                    f"📊 {current_chunk[-1]}p 묶음 처리 완료 "
                    f"({self.current_cnt}/{self.total_cnt}) "
                    f"| 키워드={kw}"
                )

            completed_keywords += 1
            processed_pages = self.current_cnt - keyword_start_cnt
            self.log_signal_func(
                f"✅ [키워드 종료 {kw_idx}/{len(keywords)}] "
                f"{kw} | 처리페이지수={processed_pages} "
                f"| 누적진행={self.current_cnt}/{self.total_cnt}"
            )

        self.log_signal_func(
            f"🏁 전체 작업 완료 "
            f"| 완료키워드수={completed_keywords}/{len(keywords)} "
            f"| 총진행={self.current_cnt}/{self.total_cnt}"
        )

        if self.db_repository and self.db_repository.status == "RUNNING":
            self.finish_job("SUCCESS")

        return True


    def handle_captcha_with_retry(self) -> int:
        captcha_cnt = self._to_int(
            self.get_setting_value(self.setting, "cpcha_cnt"),
            5,
        )
        max_tries = max(1, captcha_cnt)

        for attempt in range(1, max_tries + 1):
            if not self.running:
                self.log_signal_func(
                    "🛑 handle_captcha_with_retry 중단: running=False"
                )
                return self.CAPTCHA_FAIL

            self.log_signal_func(
                f"🔍 [시도 {attempt}/{max_tries}] 화면 상태 체크 중..."
            )

            if attempt > 1:
                pyautogui.press("tab")
                if not self.sleep_s(0.5):
                    self.log_signal_func(
                        "🛑 캡차 처리 중단: tab 이후 sleep 실패"
                    )
                    return self.CAPTCHA_FAIL

            pyperclip.copy("")
            pyautogui.hotkey("ctrl", "a")
            if not self.sleep_s(random.uniform(0.6, 0.9)):
                self.log_signal_func(
                    "🛑 캡차 처리 중단: 전체선택 후 sleep 실패"
                )
                return self.CAPTCHA_FAIL

            pyautogui.hotkey("ctrl", "c")
            if not self.sleep_s(random.uniform(0.5, 0.8)):
                self.log_signal_func(
                    "🛑 캡차 처리 중단: 복사 후 sleep 실패"
                )
                return self.CAPTCHA_FAIL

            page_content = pyperclip.paste()

            if self.is_access_limited(page_content):
                self.log_signal_func(
                    "🚫 네이버 쇼핑 접속 제한 화면을 확인했습니다."
                )
                return self.ACCESS_LIMITED

            target_text = "보안 확인을 완료해 주세요"

            if target_text not in page_content:
                if attempt == 1:
                    self.log_signal_func("✅ 캡차 없음")
                    return self.CAPTCHA_NONE

                self.log_signal_func("✅ 캡차 해결 성공!")
                return self.CAPTCHA_SOLVED

            self.log_signal_func("🚩 캡차 발견! 해결을 시작합니다.")

            # 캡챠 해결 시작
            # 6. 크롬 창의 정중앙으로 마우스 이동
            self.move_mouse_to_window_center()
            time.sleep(1)

            self.save_image_to_desktop()
        
            # 8. 컨텐츠 복사
            content = self.copy_content()

            # 9. 두 번째 탭으로 이동
            self.move_to_tab(2)
            time.sleep(1)

            self.upload_image_and_paste_content(content)
            time.sleep(1)

            self.move_to_tab(1)
        
            pyautogui.hotkey("tab")
            pyautogui.hotkey("tab")
            pyautogui.hotkey("ctrl", "v")
        
            pyautogui.hotkey("tab")
            pyautogui.hotkey("tab")
            pyautogui.hotkey("tab")
            pyautogui.press("enter")
            # 캡챠 헤결 끝
            return self.SUCCESS

        self.log_signal_func(
            f"❌ 캡차 최대 재시도 초과: {max_tries}회"
        )
        return self.CAPTCHA_FAIL


    # =========================================================
    # 목록 HTML 파싱
    # =========================================================
    def extract_items_from_html(
            self,
            html_source: str,
    ) -> List[Dict[str, Any]]:
        try:
            pattern = (
                r'<script id="__NEXT_DATA__" '
                r'type="application/json">(.*?)</script>'
            )
            match = re.search(pattern, html_source, re.DOTALL)
            if not match:
                return []

            json_data = json.loads(match.group(1))
            props = json_data.get("props", {}).get("pageProps", {})

            raw_list = (
                    props.get("compositeProducts", {}).get("list", [])
                    or props.get("initialState", {})
                    .get("products", {})
                    .get("list", [])
                    or []
            )

            normalized: List[Dict[str, Any]] = []
            for value in raw_list:
                if (
                        isinstance(value, dict)
                        and "item" in value
                        and isinstance(value.get("item"), dict)
                ):
                    normalized.append(value)
                elif isinstance(value, dict):
                    normalized.append({"item": value})

            return normalized

        except Exception as e:
            self.log_signal_func(f"⚠️ 목록 HTML 파싱 예외: {e}")
            return []
