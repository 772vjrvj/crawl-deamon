from __future__ import annotations

import json
import os
import queue
import random
import re
import sys
import threading
import time
import wave
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import pyaudiowpatch as pyaudio
import pyautogui
import pyperclip

from src.core.services import ai_whisper
from src.database.execution_repository import claim_next_rank_item, save_rank_result
from src.workers.daemon_base_worker import (
    DaemonBaseWorker,
    ExistingStoreLoader,
    ResultInserter,
)


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

    ACCESS_LIMIT_TEXTS: Tuple[str, ...] = (
        "쇼핑 서비스 접속이 일시적으로 제한되었습니다",
        "해당 네트워크의 접속을 일시적으로 제한",
        "비정상적인 접근이 감지",
    )

    # 캡차 음성 처리 단계가 장시간 멈추지 않도록 제한 시간을 둔다.
    # 녹음은 실제 녹음 시간 17초에 장치 준비 시간을 더해 25초로 제한한다.
    AUDIO_RECORD_TIMEOUT_SEC = 25.0
    WHISPER_TRANSCRIBE_TIMEOUT_SEC = 60.0

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
        self.model: Any = None
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

            # 음성 캡처 프로그램 경로 확보
            self._set_ffmpeg_path()

            # 모든 자동 조작 명령 사이에 기본적으로 0.4초씩 대기하도록 설정합니다.
            pyautogui.PAUSE = 0.4

            # 마우스를 화면 왼쪽 위 모서리로 이동하면 pyautogui 자동화가 즉시 중단되도록 설정
            pyautogui.FAILSAFE = True

            # 네이버 작업이 실행될 때만 Whisper 모델을 로딩한다.
            if self.model is None:
                ai_whisper.init("small")
                self.model = ai_whisper.get_model()
                self.log_signal_func("✅ Whisper AI (service) 연결 완료")

            self.init_flag = True
            self.log_signal_func("✅ init 완료")
            return True

        except Exception as e:
            self.log_signal_func(f"❌ 초기화 에러: {e}")
            return False

    def _set_ffmpeg_path(self) -> None:
        resource_root = self.get_resource_root()
        ffmpeg_path = os.path.join(
            resource_root,
            "resources",
            "customers",
            self.worker_name,
            "bin",
        )

        if os.path.exists(ffmpeg_path):
            os.environ["PATH"] = ffmpeg_path + os.pathsep + os.environ["PATH"]
            self.log_signal_func("✅ 환경 변수 설정 완료")
        else:
            self.log_signal_func(f"⚠️ FFmpeg 경로 없음: {ffmpeg_path}")

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

        try:
            if os.path.exists("captcha_audio_final.wav"):
                os.remove("captcha_audio_final.wav")
                self.log_signal_func("✅ [캡차 음성파일] 삭제")
        except Exception:
            pass

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

        self.model = None
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
        pyperclip.copy("chrome")
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

    def _run_with_timeout(
            self,
            func: Any,
            timeout_sec: float,
            task_name: str,
            *args: Any,
            **kwargs: Any,
    ) -> Tuple[bool, Any]:
        """
        블로킹 가능성이 있는 작업을 데몬 스레드에서 실행한다.

        제한 시간 안에 작업이 끝나면 (True, 결과)를 반환하고,
        제한 시간을 초과하거나 예외가 발생하면 (False, None)을 반환한다.

        브라우저를 어떤 URL로 다시 열지는 호출한 목록/상세 처리부가 알고 있으므로,
        여기서는 브라우저를 직접 조작하지 않고 실패 상태만 반환한다.
        """
        result_queue: queue.Queue[Tuple[str, Any]] = queue.Queue(maxsize=1)

        def runner() -> None:
            try:
                result_queue.put(
                    ("SUCCESS", func(*args, **kwargs)),
                    block=False,
                )
            except Exception as e:
                try:
                    result_queue.put(
                        ("ERROR", e),
                        block=False,
                    )
                except queue.Full:
                    pass

        thread = threading.Thread(
            target=runner,
            name=f"{self.worker_name}-{task_name}",
            daemon=True,
        )

        started_at = time.monotonic()
        self.log_signal_func(
            f"⏱️ {task_name} 시작 | 제한시간={timeout_sec:.1f}초"
        )
        thread.start()

        while self.running:
            elapsed = time.monotonic() - started_at
            remaining = timeout_sec - elapsed

            if remaining <= 0:
                self.log_signal_func(
                    f"⏰ {task_name} 제한시간 초과 "
                    f"| 경과={elapsed:.1f}초 "
                    f"| 제한={timeout_sec:.1f}초"
                )
                return False, None

            try:
                status, value = result_queue.get(
                    timeout=min(0.2, remaining)
                )
            except queue.Empty:
                continue

            elapsed = time.monotonic() - started_at

            if status == "ERROR":
                self.log_signal_func(
                    f"❌ {task_name} 예외 "
                    f"| 경과={elapsed:.1f}초 "
                    f"| 오류={value}"
                )
                return False, None

            self.log_signal_func(
                f"✅ {task_name} 완료 | 경과={elapsed:.1f}초"
            )
            return True, value

        self.log_signal_func(
            f"🛑 {task_name} 중단: running=False"
        )
        return False, None

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
            chunk_size = 10

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

    # =========================================================
    # 캡차
    # =========================================================
    def record_audio(self, filename: str, duration: int = 17) -> bool:
        audio = pyaudio.PyAudio()
        stream: Any = None

        try:
            self.log_signal_func(
                "🎧 캡차 음성 장치 확인 시작"
            )

            wasapi_info = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_speakers = audio.get_device_info_by_index(
                wasapi_info["defaultOutputDevice"]
            )

            if not default_speakers["isLoopbackDevice"]:
                for loopback in audio.get_loopback_device_info_generator():
                    if not self.running:
                        return False
                    if default_speakers["name"] in loopback["name"]:
                        default_speakers = loopback
                        break

            wave_format = pyaudio.paInt16
            channels = int(default_speakers.get("maxInputChannels") or 2)
            rate = int(default_speakers["defaultSampleRate"])
            chunk_size = 1024

            self.log_signal_func(
                f"🎧 캡차 음성 스트림 열기 "
                f"| 장치={default_speakers.get('name')} "
                f"| 채널={channels} "
                f"| rate={rate}"
            )

            stream = audio.open(
                format=wave_format,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=default_speakers["index"],
                frames_per_buffer=chunk_size,
            )

            self.log_signal_func(
                f"🎙️ 캡차 음성 녹음 시작 | duration={duration}초"
            )

            frames: List[bytes] = []
            required_chunk_count = int(
                rate / chunk_size * duration
            )

            # stream.read()가 오디오 장치 문제로 장시간 블로킹되는 상황을 줄이기 위해
            # 읽을 수 있는 데이터가 확보된 경우에만 stream.read()를 호출한다.
            internal_deadline = time.monotonic() + duration + 5.0

            while len(frames) < required_chunk_count:
                if not self.running:
                    return False

                if time.monotonic() >= internal_deadline:
                    self.log_signal_func(
                        "⏰ 캡차 음성 녹음 내부 제한시간 초과 "
                        f"| 수집={len(frames)}/{required_chunk_count}"
                    )
                    return False

                available_frames = stream.get_read_available()

                if available_frames < chunk_size:
                    time.sleep(0.02)
                    continue

                frames.append(
                    stream.read(
                        chunk_size,
                        exception_on_overflow=False,
                    )
                )

            with wave.open(filename, "wb") as wave_file:
                wave_file.setnchannels(channels)
                wave_file.setsampwidth(
                    audio.get_sample_size(wave_format)
                )
                wave_file.setframerate(rate)
                wave_file.writeframes(b"".join(frames))

            self.log_signal_func(
                f"✅ 캡차 음성 녹음 파일 저장 완료 "
                f"| 파일={filename} "
                f"| chunk={len(frames)}"
            )
            return True

        except Exception as e:
            self.log_signal_func(f"❌ 캡차 음성 녹음 예외: {e}")
            return False

        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass

                try:
                    stream.close()
                except Exception:
                    pass

            audio.terminate()

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

            if attempt == 1:
                for _ in range(5):
                    pyautogui.press("tab")
                    if not self.sleep_s(random.uniform(0.1, 0.2)):
                        self.log_signal_func(
                            "🛑 캡차 처리 중단: tab 이동 중 sleep 실패"
                        )
                        return self.CAPTCHA_FAIL
                pyautogui.press("enter")
            else:
                pyautogui.press("enter")

            # 음성 재생이 시작되기 전 짧게 대기한다.
            if not self.sleep_s(0.4):
                self.log_signal_func(
                    "🛑 캡차 처리 중단: 음성 재생 전 sleep 실패"
                )
                return self.CAPTCHA_FAIL

            filename = "captcha_audio_final.wav"

            # 오디오 장치 또는 stream.read()가 장시간 응답하지 않는 상황을 막는다.
            record_finished, record_success = self._run_with_timeout(
                self.record_audio,
                self.AUDIO_RECORD_TIMEOUT_SEC,
                "캡차 음성 녹음",
                filename,
                17,
            )

            if not record_finished or not record_success:
                if not self.running:
                    return self.CAPTCHA_FAIL

                self.log_signal_func(
                    "🔄 캡차 음성 녹음이 제한시간 안에 완료되지 않았거나 "
                    "실패했습니다. 브라우저 재시작 흐름으로 전환합니다."
                )
                # 목록/상세 호출부에서 현재 URL에 맞게
                # 브라우저를 닫고 다시 실행한다.
                return self.ACCESS_LIMITED

            # Whisper 분석이 장시간 응답하지 않는 상황을 막는다.
            transcribe_finished, result = self._run_with_timeout(
                self.model.transcribe,
                self.WHISPER_TRANSCRIBE_TIMEOUT_SEC,
                "Whisper 음성 분석",
                filename,
                language="ko",
                fp16=False,
            )

            if not transcribe_finished:
                if not self.running:
                    return self.CAPTCHA_FAIL

                self.log_signal_func(
                    "🔄 Whisper 음성 분석이 제한시간 안에 완료되지 않았습니다. "
                    "브라우저 재시작 흐름으로 전환합니다."
                )
                # 목록/상세 호출부에서 현재 URL에 맞게
                # 브라우저를 닫고 다시 실행한다.
                return self.ACCESS_LIMITED

            code = "".join(
                filter(str.isdigit, result["text"])
            )[:6]

            if not code:
                code = "123456"
                self.log_signal_func(
                    "⚠️ AI 인식 실패 (숫자 없음). "
                    "기본값 '123456' 입력"
                )
            else:
                self.log_signal_func(f"📝 AI 인식 코드: {code}")

            if attempt == 1:
                pyautogui.press("tab")
                if not self.sleep_s(0.5):
                    self.log_signal_func(
                        "🛑 캡차 처리 중단: 입력칸 이동 후 sleep 실패"
                    )
                    return self.CAPTCHA_FAIL
            else:
                pyautogui.hotkey("shift", "tab")
                if not self.sleep_s(0.5):
                    self.log_signal_func(
                        "🛑 캡차 처리 중단: shift+tab 후 sleep 실패"
                    )
                    return self.CAPTCHA_FAIL

            pyautogui.write(
                code,
                interval=random.uniform(0.1, 0.2),
            )

            for _ in range(3):
                pyautogui.press("tab")

            pyautogui.press("enter")

            self.log_signal_func("⏳ 결과 검증 대기 중...")
            if not self.sleep_s(random.uniform(5.0, 6.0)):
                self.log_signal_func(
                    "🛑 캡차 처리 중단: 결과 검증 대기 sleep 실패"
                )
                return self.CAPTCHA_FAIL

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
