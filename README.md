# crawl-deamon

하나의 코드와 EXE로 여러 서비스 Worker를 실행하는 MariaDB 큐 기반 데몬입니다.
각 실행 인스턴스는 `.env`의 `DAEMON_SERVICE_CODE`에 지정된 서비스만 처리합니다.

## 지원 서비스

- `PANDA_MESSAGE`
  - 기존 Selenium/undetected-chromedriver 방식 그대로 실행
  - 팬더 로그인, 랭킹 조회, 메시지 발송
- `NAVER_SHOP_TOTAL_DETAIL`
  - Selenium을 사용하지 않음
  - PyAutoGUI로 Windows의 일반 Chrome을 직접 실행하고 제어
  - 네이버 쇼핑 상품과 스토어 전체 방문자 수 수집
  - Whisper 음성 인식으로 캡차 처리

## 공통 실행 흐름

1. MariaDB 연결
2. 5초마다 담당 서비스의 `READY` 작업 조회
3. `USER_SERVICE`, `SERVICE_MASTER`를 조인하여 `SERVICE_CODE`가 일치하는 작업만 선점
4. `SERVICE_EXECUTION_HIST.SETTING_JSON`을 실행 설정으로 사용
5. 서비스별 Worker 실행
6. 상세 결과를 `SERVICE_EXECUTION_RESULT`에 저장
7. 실행 이력을 `SUCCESS` 또는 `FAIL`로 마감

예약 이후 `USER_SERVICE.SETTING_JSON`이 변경되더라도 예약 당시 스냅샷인
`SERVICE_EXECUTION_HIST.SETTING_JSON`으로 실행됩니다.

## 환경 설정

`.env.example`을 `.env`로 복사하고 실제 DB 접속 정보를 입력합니다.

```env
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=service_db
DB_USER=service_user
DB_PASSWORD=change-me
DB_CHARSET=utf8mb4
DB_CONNECT_TIMEOUT=10
DAEMON_SERVICE_CODE=NAVER_SHOP_TOTAL_DETAIL
SELENIUM_HEADLESS=true
```

팬더 전용 인스턴스는 다음 값으로 실행합니다.

```env
DAEMON_SERVICE_CODE=PANDA_MESSAGE
SELENIUM_HEADLESS=true
```

`SELENIUM_HEADLESS`는 팬더 Selenium에만 적용됩니다. 기본값은 `true`이며,
네이버 쇼핑 Worker는 이 값과 관계없이 PyAutoGUI로 일반 Chrome을 직접 띄웁니다.

## 네이버 쇼핑 설정

```json
{
  "keyword": "패션잡화,화장품",
  "start_page": 1,
  "end_page": 5,
  "site_total_cnt": 500,
  "cpcha_cnt": 30,
  "dup_yn": true
}
```

- `keyword`: 쉼표로 구분하며 중복 키워드도 입력 순서대로 반복 실행
- `start_page`, `end_page`: 조회 페이지 범위
- `site_total_cnt`: 전체 방문자 수가 이 값 이하인 결과만 저장
- `cpcha_cnt`: 캡차 최대 시도 횟수(기존 설정명 호환을 위해 철자 유지)
- `dup_yn`: 같은 `USER_SERVICE_ID`의 과거 전체 성공 결과와 현재 실행에서 스토어명 중복 제거

수집 결과 한 건은 `SERVICE_EXECUTION_RESULT.RESULT_JSON` 한 건으로 저장됩니다.

## FFmpeg 위치

기존 프로그램의 다음 파일을 직접 복사합니다.

```text
E:\git\crawl-program\resources\customers\naver_shop_total_detail\bin\ffmpeg.exe
```

데몬 프로젝트에서는 아래 위치에 둡니다.

```text
resources\customers\naver_shop_total_detail\bin\ffmpeg.exe
```

EXE 배포 폴더에서도 `crawl-deamon.exe`를 기준으로 위 상대 경로가 유지되어야 합니다.

## 개발 실행

권장 환경은 Windows 10/11 및 Python 3.11입니다.

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

`NAVER_SHOP_TOTAL_DETAIL` 실행 중에는 키보드와 마우스를 자동 제어하므로
작업 PC를 동시에 조작하지 않아야 합니다. PyAutoGUI 안전 중단은 마우스를 화면
왼쪽 위 모서리로 이동하면 동작합니다.

Whisper `small` 모델은 네이버 쇼핑 작업이 처음 실행될 때 로딩됩니다.
최초 실행 환경에 모델 캐시가 없다면 모델 다운로드가 필요할 수 있습니다.

## PyInstaller 빌드

```powershell
cd E:\git\crawl-deamon
.\venv\Scripts\Activate.ps1
pip install -r requirements-build.txt

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force crawl-deamon.spec -ErrorAction SilentlyContinue

python -m PyInstaller `
--noconfirm `
--clean `
--onedir `
--contents-directory . `
--console `
--name crawl-deamon `
--paths "E:\git\crawl-deamon" `
--collect-all selenium `
--collect-all undetected_chromedriver `
--collect-all whisper `
--collect-all torch `
--collect-all pyaudiowpatch `
--hidden-import pyautogui `
--hidden-import pyperclip `
main.py
```

빌드 후 설정과 FFmpeg를 복사합니다.

```powershell
Copy-Item .env .\dist\crawl-deamon\.env -Force

New-Item `
  -ItemType Directory `
  -Force `
  .\dist\crawl-deamon\resources\customers\naver_shop_total_detail\bin

Copy-Item `
  .\resources\customers\naver_shop_total_detail\bin\ffmpeg.exe `
  .\dist\crawl-deamon\resources\customers\naver_shop_total_detail\bin\ffmpeg.exe `
  -Force
```

## 실행 인스턴스 분리

코드와 EXE는 같지만 서비스별 배포 폴더와 `.env`를 분리하는 것을 권장합니다.

```text
C:\daemon\panda\
  crawl-deamon.exe
  .env  (DAEMON_SERVICE_CODE=PANDA_MESSAGE, SELENIUM_HEADLESS=true)

C:\daemon\naver-shop\
  crawl-deamon.exe
  .env  (DAEMON_SERVICE_CODE=NAVER_SHOP_TOTAL_DETAIL)
  resources\customers\naver_shop_total_detail\bin\ffmpeg.exe
```

이렇게 실행하면 한 서비스의 장시간 작업이나 장애가 다른 서비스 큐를 막지 않습니다.
팬더 Chrome은 headless로 실행되고 네이버만 일반 Chrome 창을 표시합니다.
