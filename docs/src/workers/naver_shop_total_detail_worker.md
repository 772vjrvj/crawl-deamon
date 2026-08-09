NAVER_SHOP_TOTAL_DETAIL 빌드 및 실행

네이버 쇼핑 데몬은 일반 Chrome을 PyAutoGUI로 조작하고, 캡차 음성을 PyAudioWPatch와 Whisper로 처리한다. Selenium과 undetected-chromedriver는 사용하지 않는다.

1. 준비 사항

Windows 10/11 64비트

Python 3.11 64비트 권장

Google Chrome 설치

화면 자동화를 실행할 로그인된 Windows 데스크톱 세션

ffmpeg 실행 파일

최신 통합 데몬 소스와 구형 MariaDB 호환 중복조회 코드

PyAutoGUI가 실제 키보드와 마우스를 사용하므로 Windows 서비스의 Session 0이나 잠긴 화면에서 실행하면 안 된다. 실행 중에는 해당 PC의 키보드·마우스를 조작하지 않는 것이 안전하다.

2. 네이버 전용 가상환경과 패키지 설치

PowerShell에서 프로젝트 루트로 이동한 뒤 실행한다.

cd E:\git\crawl-deamon

py -3.11 -m venv .venv-naver-shop
.\.venv-naver-shop\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r .\requirements-naver-shop.txt

설치 확인:

python -c "import pymysql, pyaudiowpatch, pyautogui, pyperclip, whisper; print('NAVER 패키지 확인 완료')"
python -m PyInstaller --version

3. Whisper small 모델 미리 받기

빌드 전, 실제 데몬을 실행할 Windows 사용자 계정으로 모델을 한 번 내려받는다.

python -c "import whisper; whisper.load_model('small'); print('Whisper small 모델 준비 완료')"

기본 캐시 위치:

%USERPROFILE%\.cache\whisper\small.pt

다른 PC로 배포할 경우 다음 중 하나가 필요하다.

대상 PC에서 위 명령을 실행해 모델을 다운로드한다.

small.pt를 대상 PC의 같은 사용자 캐시 경로에 복사한다.

Whisper 모델은 EXE 내부에 자동 포함되지 않는다. 대상 PC가 인터넷에 연결되지 않았다면 반드시 모델 파일을 미리 복사한다.

4. 네이버 EXE 빌드

아래 명령은 네이버에 필요한 Whisper/Torch/오디오 모듈을 포함하고 팬더의 Selenium 모듈을 제외한다.

cd E:\git\crawl-deamon
.\.venv-naver-shop\Scripts\Activate.ps1

Remove-Item -Recurse -Force .\build\crawl-deamon-naver-shop -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .\dist\crawl-deamon-naver-shop -ErrorAction SilentlyContinue
Remove-Item -Force .\crawl-deamon-naver-shop.spec -ErrorAction SilentlyContinue

python -m PyInstaller `
  --noconfirm `
--clean `
  --onedir `
--contents-directory _internal `
  --console `
--name crawl-deamon-naver-shop `
  --paths "E:\git\crawl-deamon" `
--collect-all whisper `
  --collect-all torch `
--collect-all pyaudiowpatch `
  --hidden-import pyautogui `
--hidden-import pyperclip `
  --hidden-import src.core.services.ai_whisper `
--hidden-import src.workers.naver_shop_total_detail_worker `
  --exclude-module src.workers.panda_worker `
--exclude-module selenium `
  --exclude-module undetected_chromedriver `
.\main.py

Whisper와 Torch가 포함되므로 팬더 빌드보다 빌드 시간과 결과 용량이 훨씬 크다.

완료 위치:

E:\git\crawl-deamon\dist\crawl-deamon-naver-shop\
└─ crawl-deamon-naver-shop.exe

5. ffmpeg 수동 배치

사용 중인 ffmpeg 원본:

E:\git\crawl-program\resources\customers\naver_shop_total_detail\bin\ffmpeg.exe

빌드 결과 폴더 아래에 다음 경로를 만들고 ffmpeg.exe를 복사한다.

$targetBin = "E:\git\crawl-deamon\dist\crawl-deamon-naver-shop\resources\customers\naver_shop_total_detail\bin"
New-Item -ItemType Directory -Force $targetBin
Copy-Item "E:\git\crawl-program\resources\customers\naver_shop_total_detail\bin\ffmpeg.exe" "$targetBin\ffmpeg.exe" -Force

최종 위치:

dist\crawl-deamon-naver-shop\resources\customers\naver_shop_total_detail\bin\ffmpeg.exe

6. 네이버용 .env 배치

빌드 결과 폴더에 .env를 만들고 DB 접속정보와 서비스 코드를 입력한다.

DB_HOST=서버주소
DB_PORT=3306
DB_NAME=DB명
DB_USER=계정
DB_PASSWORD=비밀번호
DB_CHARSET=utf8mb4
DB_CONNECT_TIMEOUT=10

DAEMON_SERVICE_CODE=NAVER_SHOP_TOTAL_DETAIL
SELENIUM_HEADLESS=true

SELENIUM_HEADLESS는 공통 .env 형식을 맞추기 위해 둘 수 있지만 네이버 Worker에는 적용되지 않는다. 네이버는 화면 매크로이므로 반드시 일반 Chrome을 직접 띄운다.

.env 위치는 반드시 EXE와 같은 폴더여야 한다.

dist\crawl-deamon-naver-shop\.env
dist\crawl-deamon-naver-shop\crawl-deamon-naver-shop.exe

7. 실행과 확인

로그인된 Windows 데스크톱에서 실행한다.

cd E:\git\crawl-deamon\dist\crawl-deamon-naver-shop
.\crawl-deamon-naver-shop.exe

정상 시작 로그:

[DAEMON] 담당 SERVICE_CODE=NAVER_SHOP_TOTAL_DETAIL
[DB] MariaDB 연결 성공
[BROWSER] SELENIUM_HEADLESS는 적용하지 않습니다. Worker가 일반 Chrome을 직접 제어합니다.
[BROWSER] Worker가 일반 Chrome을 직접 제어합니다.

첫 실제 작업에서 확인할 사항:

✅ 환경 변수 설정 완료가 출력되는지 확인한다. 경로 없음이 출력되면 ffmpeg 배치를 다시 확인한다.

Whisper small 모델이 정상 로딩되는지 확인한다.

Windows 기본 출력 장치를 통해 Chrome 오디오가 재생되는지 확인한다.

화면이 잠기지 않았고 Chrome 창이 실제로 열리는지 확인한다.

같은 USER_SERVICE_ID의 과거 결과 중복 제거가 동작하는지 확인한다.

구형 MariaDB에서는 JSON_UNQUOTE, JSON_EXTRACT를 SQL에 사용하지 않고 RESULT_JSON을 Python에서 파싱해야 한다.

8. 배포 파일

다른 PC로 옮길 때는 아래 항목을 함께 옮긴다.

dist\crawl-deamon-naver-shop\ 전체
%USERPROFILE%\.cache\whisper\small.pt  (대상 PC에서 다운로드하지 않을 경우)

EXE 하나만 복사하면 Torch DLL, Python 라이브러리, ffmpeg가 빠져 실행되지 않는다.

9. 팬더 데몬과 동시 운영

팬더: dist\crawl-deamon-panda\, DAEMON_SERVICE_CODE=PANDA_MESSAGE, SELENIUM_HEADLESS=true

네이버: dist\crawl-deamon-naver-shop\, DAEMON_SERVICE_CODE=NAVER_SHOP_TOTAL_DETAIL (headless 미적용)

두 프로세스 모두 최신 통합 데몬으로 교체한다.

각 프로세스는 자신의 서비스 코드에 해당하는 READY 작업만 선점해야 한다.

로그 폴더와 Chrome 프로필이 공유되는 구조라면 동시에 같은 파일을 쓰지 않는지 추가로 확인한다.