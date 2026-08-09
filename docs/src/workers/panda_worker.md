PANDA_MESSAGE 빌드 및 실행

팬더 데몬은 Selenium과 undetected-chromedriver를 사용한다. Whisper, 오디오 캡처, PyAutoGUI, ffmpeg는 포함하지 않는다.

1. 준비 사항

Windows 10/11 64비트

Python 3.11 64비트 권장

Google Chrome 설치

최신 통합 데몬 소스 사용

기존에 실행 중인 구버전 팬더 데몬 종료

팬더와 네이버를 동시에 운영할 때는 반드시 각각 다른 배포 폴더와 .env를 사용한다. 구버전 데몬이 남아 있으면 다른 서비스 작업을 잘못 선점할 수 있다.

2. 팬더 전용 가상환경과 패키지 설치

PowerShell에서 프로젝트 루트로 이동한 뒤 실행한다.

cd E:\git\crawl-deamon

py -3.11 -m venv .venv-panda
.\.venv-panda\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r .\requirements-panda.txt

설치 확인:

python -c "import pymysql, selenium, undetected_chromedriver; print('PANDA 패키지 확인 완료')"
python -m PyInstaller --version

3. 팬더 EXE 빌드

아래 명령은 팬더에 필요한 모듈만 포함하고 네이버 전용 패키지를 제외한다.

cd E:\git\crawl-deamon
.\.venv-panda\Scripts\Activate.ps1

Remove-Item -Recurse -Force .\build\crawl-deamon-panda -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .\dist\crawl-deamon-panda -ErrorAction SilentlyContinue
Remove-Item -Force .\crawl-deamon-panda.spec -ErrorAction SilentlyContinue

python -m PyInstaller `
  --noconfirm `
--clean `
  --onedir `
--contents-directory . `
  --console `
--name crawl-deamon-panda `
  --paths "E:\git\crawl-deamon" `
--collect-all selenium `
  --collect-all undetected_chromedriver `
--hidden-import src.workers.panda_worker `
  --exclude-module src.workers.naver_shop_total_detail_worker `
--exclude-module whisper `
  --exclude-module torch `
--exclude-module pyaudiowpatch `
  --exclude-module pyautogui `
--exclude-module pyperclip `
.\main.py

완료 위치:

E:\git\crawl-deamon\dist\crawl-deamon-panda\
└─ crawl-deamon-panda.exe

4. 팬더용 .env 배치

빌드 결과 폴더에 .env를 만들고 DB 접속정보와 서비스 코드를 입력한다.

DB_HOST=서버주소
DB_PORT=3306
DB_NAME=DB명
DB_USER=계정
DB_PASSWORD=비밀번호
DB_CHARSET=utf8mb4
DB_CONNECT_TIMEOUT=10

DAEMON_SERVICE_CODE=PANDA_MESSAGE
SELENIUM_HEADLESS=true

SELENIUM_HEADLESS=true: Chrome 창을 표시하지 않는다. 한 PC에서 네이버 데몬과 같이 운영할 때 권장한다.

SELENIUM_HEADLESS=false: 점검할 때만 Chrome 창을 표시한다.

값이 없으면 안전한 기본값인 true로 실행된다.

.env 위치는 반드시 EXE와 같은 폴더여야 한다.

dist\crawl-deamon-panda\.env
dist\crawl-deamon-panda\crawl-deamon-panda.exe

5. 실행과 확인

cd E:\git\crawl-deamon\dist\crawl-deamon-panda
.\crawl-deamon-panda.exe

정상 시작 로그:

[DAEMON] 담당 SERVICE_CODE=PANDA_MESSAGE
[SELENIUM] HEADLESS=true
[DB] MariaDB 연결 성공
[QUEUE] 5초마다 READY 작업을 확인합니다.

확인할 사항:

SERVICE_EXECUTION_HIST에 PANDA_MESSAGE 서비스의 READY 작업이 있어야 한다.

SETTING_JSON의 ranking_type은 팬더가 지원하는 값이어야 한다.

지원하지 않는 ranking_type입니다:가 빈 값으로 나오면 구버전 데몬 또는 잘못된 작업 선점 여부를 먼저 확인한다.

팬더와 네이버 프로세스를 동시에 실행할 경우 두 로그의 SERVICE_CODE가 서로 다른지 확인한다.

운영 중 팬더 Chrome 창이 나타나면 팬더 배포 폴더의 .env에서 SELENIUM_HEADLESS=true인지 확인한다.

6. 배포 파일

다른 PC로 옮길 때는 crawl-deamon-panda.exe 하나만 복사하지 말고 아래 폴더 전체를 복사한다.

dist\crawl-deamon-panda\ 전체

팬더 빌드에는 resources\...\ffmpeg.exe가 필요하지 않다.