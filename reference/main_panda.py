# -*- coding: utf-8 -*-
"""
팬더라이브 신입 BJ 랭킹 조회 및 메시지 전송 테스트

실행 흐름
"""

import json
import sys
import time
import random
import string
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Any, Dict, List
from urllib.parse import urlencode
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.selenium_utils import SeleniumUtils


PANDA_LIVE_URL = "https://www.pandalive.co.kr/"

# RANKING_BJ_URL = "https://www.pandalive.co.kr/ranking/rankingPersonalBJ" # 개인
# RANKING_BJ_URL = "https://www.pandalive.co.kr/ranking/rankingNewBJ" # 신입
# RANKING_BJ_URL = "https://www.pandalive.co.kr/ranking/rankingCrewBJ" # 크루
RANKING_BJ_URL = "https://www.pandalive.co.kr/ranking/rankingPopular" #  전체



RANKING_API_URL = "https://api.pandalive.co.kr/v1/live/cache"
SEND_MESSAGE_API_URL = "https://api.pandalive.co.kr/v1/post/send_message"

PAGE_SIZE = 20
PAGE_COUNT = 20

# 사용할 메시지 문구 후보들
MESSAGE_POOL = [
    "안녕하세요! 좋은 하루 되세요.",
    "반갑습니다!",
    "안녕하세요",
    "오늘도 행복한 하루 보내시길 바랍니다.",
    "감사합니다."
]

OUTPUT_JSON_PATH = PROJECT_ROOT / "ranking_new_bj_list.json"


def show_login_confirmation() -> None:
    """사용자가 브라우저에서 직접 로그인할 때까지 안내 팝업을 표시한다."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    root.lift()
    root.focus_force()

    try:
        messagebox.showinfo(
            title="팬더라이브 로그인",
            message=(
                "브라우저에서 팬더라이브 로그인을 완료해 주세요.\n\n"
                "로그인이 완료되면 이 창의 [확인] 버튼을 누르세요."
            ),
            parent=root,
        )
    finally:
        root.destroy()


def post_form_with_browser(
        driver,
        url: str,
        form_data: Dict[str, Any],
        timeout_sec: int = 30,
) -> Dict[str, Any]:
    """
    로그인된 Chrome 브라우저 안에서 fetch POST 요청을 실행한다.
    브라우저의 로그인 쿠키 및 헤더를 그대로 사용하기 위해 JS fetch를 사용한다.
    """
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
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
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

    result = driver.execute_async_script(script, url, encoded_body)

    if not isinstance(result, dict):
        raise RuntimeError(f"API 응답 형식이 올바르지 않습니다: {result}")

    if not result.get("ok"):
        status = result.get("status")
        response_data = result.get("data")

        if isinstance(response_data, dict):
            error_message = response_data.get("message")

            if not error_message:
                error_message = json.dumps(
                    response_data,
                    ensure_ascii=False,
                )
        else:
            error_message = (
                    result.get("error")
                    or result.get("text")
                    or result.get("statusText")
                    or "알 수 없는 오류"
            )

        raise RuntimeError(
            f"API 요청 실패: status={status}, message={error_message}"
        )

    data = result.get("data")

    if not isinstance(data, dict):
        response_text = result.get("text", "")
        raise RuntimeError(f"JSON 응답이 아닙니다: {response_text[:500]}")

    return data


def fetch_ranking_page(driver, page_number: int) -> List[Dict[str, Any]]:
    """신입 BJ 랭킹 한 페이지를 조회한다."""
    offset = (page_number - 1) * PAGE_SIZE
    payload = {
        "type": "rankingNewBJ",
        "limit": PAGE_SIZE,
        "offset": offset,
    }

    print(f"\n[랭킹 조회] page={page_number}, limit={PAGE_SIZE}, offset={offset}")

    response_json = post_form_with_browser(
        driver=driver,
        url=RANKING_API_URL,
        form_data=payload,
    )

    items = response_json.get("list", [])
    if not isinstance(items, list):
        raise RuntimeError(f"{page_number}페이지 응답의 list가 배열이 아닙니다.")

    print(f"조회 결과: {len(items)}개")
    return items


def fetch_all_rankings(driver) -> List[Dict[str, Any]]:
    """1~5페이지를 조회한 뒤 userIdx 기준으로 중복 제거한다."""
    all_items: List[Dict[str, Any]] = []

    for page_number in range(1, PAGE_COUNT + 1):
        page_items = fetch_ranking_page(driver, page_number)

        if len(page_items) <= 0:
            break

        all_items.extend(page_items)

        if page_number < PAGE_COUNT:
            time.sleep(0.5)

    unique_by_user_idx: Dict[int, Dict[str, Any]] = {}
    no_user_idx_items: List[Dict[str, Any]] = []

    for item in all_items:
        if not isinstance(item, dict):
            continue

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

    unique_items = list(unique_by_user_idx.values())
    unique_items.extend(no_user_idx_items)

    duplicate_count = len(all_items) - len(unique_items)

    print("\n" + "=" * 70)
    print(f"전체 조회 개수       : {len(all_items)}개")
    print(f"userIdx 중복 제거 수 : {duplicate_count}개")
    print(f"최종 LIST 개수       : {len(unique_items)}개")
    print("=" * 70)

    return unique_items


def save_rankings(items: List[Dict[str, Any]]) -> None:
    """한글이 보이도록 JSON 파일로 저장한다."""
    with OUTPUT_JSON_PATH.open("w", encoding="utf-8") as file:
        json.dump(items, file, ensure_ascii=False, indent=2)

    print(f"\n랭킹 LIST 저장 완료: {OUTPUT_JSON_PATH}")


def generate_unique_message(pool: List[str]) -> str:
    """
    메시지 문구를 랜덤으로 선택하고, 뒤에 무작위 4자리 난수를 붙여
    완전 동일한 문장이 되지 않도록 생성합니다.
    """
    base_msg = random.choice(pool)
    random_code = ''.join(random.choices(string.ascii_letters + string.digits, k=4))
    return f"{base_msg} [{random_code}]"


def send_message_payloads(driver, items: List[Dict[str, Any]]) -> None:
    """
    브라우저 세션(쿠키, Origin, Referer 등)을 그대로 활용하여 POST 메시지를 전송합니다.
    """
    print("\n" + "=" * 70)
    print("[LIVE RUN] 실제 메시지 전송을 시작합니다.")
    print("요청 URL:", SEND_MESSAGE_API_URL)
    print("요청 메서드: POST")
    print("=" * 70)

    sent_count = 0

    for index, item in enumerate(items, start=1):

        user_idx = item.get("userIdx")
        user_nick = item.get("userNick", "")
        user_id = item.get("userId", "")

        if user_idx is None:
            print(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                f"[{index}] userIdx 없음 - 제외 "
                f"(userNick={user_nick}, userId={user_id})"
            )
            continue

        # 중복 방지용 랜덤 메시지 생성
        random_message = generate_unique_message(MESSAGE_POOL)

        payload = {
            "message": random_message,
            "userIdx": user_idx,
        }

        try:
            # 브라우저 내부에서 POST 전송 (쿠키 및 헤더 자동 포함)
            response_json = post_form_with_browser(
                driver=driver,
                url=SEND_MESSAGE_API_URL,
                form_data=payload,
            )

            print(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                f"[{index}] 전송 성공 | "
                f"userNick={user_nick}, "
                f"userIdx={user_idx} | "
                f"msg='{random_message}' | "
                f"응답={response_json}"
            )
            sent_count += 1

        except Exception as e:
            error_message = str(e)
            print(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                f"[{index}] 전송 실패 | userIdx={user_idx} | 에러: {e}"
            )

            if "쪽지 전송이 제한되었습니다" in error_message:
                print(
                    f"\n[전송 중단] 계정의 쪽지 발송이 제한되었습니다. "
                    f"성공 건수={sent_count}, 현재 순번={index}"
                )
                break


        # 2~4초 사이 랜덤 대기
        delay = random.uniform(5, 7)
        time.sleep(delay)

    print("\n" + "=" * 70)
    print(f"전송 성공 메시지: {sent_count}개")
    print("=" * 70)


def main() -> None:
    selenium_utils = SeleniumUtils(headless=False, debug=True)

    try:
        driver = selenium_utils.start_driver(
            timeout=30,
            view_mode="browser",
            window_size=(1200, 900),
        )

        print("[1/5] 팬더라이브에 접속합니다.")
        driver.get(PANDA_LIVE_URL)
        selenium_utils.wait_ready_state_complete(timeout_sec=15)

        print("[2/5] 브라우저에서 직접 로그인해 주세요.")
        show_login_confirmation()

        input("\n[3/5] 로그인이 완료되었다면 Enter 키를 눌러 랭킹 조회를 시작하세요.")

        print("\n[4/5] 신입 BJ 랭킹 페이지로 이동합니다.")
        driver.get(RANKING_BJ_URL)
        selenium_utils.wait_ready_state_complete(timeout_sec=15)

        print("현재 URL:", driver.current_url)
        print("현재 제목:", driver.title)

        ranking_items = fetch_all_rankings(driver)
        save_rankings(ranking_items)

        print("\n[5/5] 브라우저 세션을 이용해 메시지를 전송합니다.")
        send_message_payloads(driver, ranking_items)

        input("\n작업이 끝났습니다. 브라우저를 종료하려면 Enter 키를 누르세요.")

    except KeyboardInterrupt:
        print("\n사용자가 실행을 중단했습니다.")

    except Exception as e:
        print(f"\n실행 중 오류가 발생했습니다: {e}")
        raise

    finally:
        selenium_utils.quit()
        print("브라우저를 종료했습니다.")


if __name__ == "__main__":
    main()