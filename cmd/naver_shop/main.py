import os
import time
import winreg

import pyautogui
import pyperclip
from dotenv import load_dotenv
import pygetwindow as gw


def open_chrome() -> None:
    """일반 크롬을 새 창으로 실행한다."""
    pyautogui.hotkey("win", "r")
    time.sleep(0.5)

    pyperclip.copy("chrome --new-window about:blank")
    pyautogui.hotkey("ctrl", "v")
    pyautogui.press("enter")



def move_to_url(url: str) -> None:
    """현재 탭에서 지정한 URL로 이동한다."""
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.2)

    pyperclip.copy(url)
    pyautogui.hotkey("ctrl", "v")
    pyautogui.press("enter")


def open_new_tab(url: str) -> None:
    """새 탭을 열고 지정한 URL로 이동한다."""
    pyautogui.hotkey("ctrl", "t")
    time.sleep(0.5)

    move_to_url(url)


def move_to_tab(num: int) -> None:
    """지정한 번호의 크롬 탭으로 이동한다."""
    if not 1 <= num <= 8:
        raise ValueError("탭 번호는 1부터 8까지 입력해야 합니다.")

    pyautogui.hotkey("ctrl", str(num))


def move_mouse_to_window_center():
    """현재 활성화된 창의 정중앙으로 마우스를 이동한다."""
    window = gw.getActiveWindow()

    if window is None:
        raise RuntimeError("활성화된 창을 찾지 못했습니다.")

    center_x = window.left + window.width // 2
    center_y = window.top + window.height // 2

    pyautogui.moveTo(center_x, center_y, duration=0.5)

    print(f"마우스 이동 완료: x={center_x}, y={center_y}")


def get_desktop_path() -> str:
    """Windows에 설정된 실제 바탕화면 경로를 가져온다."""
    registry_path = (
        r"Software\Microsoft\Windows\CurrentVersion"
        r"\Explorer\Shell Folders"
    )

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
        desktop_path, _ = winreg.QueryValueEx(key, "Desktop")

    return desktop_path

def save_image_to_desktop():
    """마우스 위치의 이미지를 우클릭 메뉴를 통해 바탕화면에 저장한다."""
    save_path = os.path.join(get_desktop_path(), "test_test_test.png")

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

    print(f"이미지 저장 시도: {save_path}")


def copy_content() -> str:

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


def upload_image_and_paste_content(content: str) -> None:
    # 바탕화면의 test.png 경로
    image_path = os.path.join(get_desktop_path(), "test_test_test.png")

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

    time.sleep(15)


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


def main() -> None:

    # 1. 설정 파일 일기
    # main.py와 같은 위치의 .env 파일 읽기
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base_dir, ".env")
    load_dotenv(env_path)

    site_one = os.getenv("SITE_ONE")
    site_two = os.getenv("SITE_TWO")

    if not site_one or not site_two:
        raise ValueError(".env 파일에 SITE_ONE과 SITE_TWO를 설정해주세요.")

    # 2. 크롬 새 창 실행
    open_chrome()
    time.sleep(3)

    # 3. 첫 번째 탭에서 SITE_ONE 열기
    move_to_url(site_one)
    time.sleep(2)

    # 4. 두 번째 탭에서 SITE_TWO 열기
    open_new_tab(site_two)
    time.sleep(2)

    # 5. 첫 번째 탭으로 이동
    move_to_tab(1)
    time.sleep(1)
    print("사이트 2개를 열고 첫 번째 탭으로 이동했습니다.")

    # 6. 크롬 창의 정중앙으로 마우스 이동
    move_mouse_to_window_center()
    time.sleep(1)

    # 7. 이미지 저장
    save_image_to_desktop()
    print("이미지를 바탕화면에 저장했습니다.")


    # 8. 컨텐츠 복사
    content = copy_content()

    print(f"추출한 내용: {content}")

    # 9. 두 번째 탭으로 이동
    move_to_tab(2)
    time.sleep(1)

    upload_image_and_paste_content(content)
    time.sleep(1)

    move_to_tab(1)

    pyautogui.hotkey("tab")
    pyautogui.hotkey("tab")
    pyautogui.hotkey("ctrl", "v")

    pyautogui.hotkey("tab")
    pyautogui.hotkey("tab")
    pyautogui.hotkey("tab")
    pyautogui.press("enter")

if __name__ == "__main__":
    main()