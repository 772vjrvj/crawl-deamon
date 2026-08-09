# crawl-deamon 실행 및 빌드

## 1. 프로그램 개요

`crawl-deamon`은 MariaDB의 예약 실행 작업을 조회하여 팬더라이브 랭킹 대상에게 메시지를 발송하는 데몬 프로그램이다.

실행 흐름:

1. MariaDB 연결
2. 5초마다 `READY` 작업 조회
3. 실행할 작업을 `RUNNING`으로 선점
4. 작업마다 Chrome 실행
5. `SETTING_JSON`의 아이디와 비밀번호로 자동 로그인
6. 랭킹 대상 조회
7. 상세 결과를 `READY` 또는 `SKIP`으로 먼저 저장
8. 대상별 메시지 발송 후 `SUCCESS` 또는 `FAIL`로 변경
9. 작업 이력을 `SUCCESS` 또는 `FAIL`로 변경
10. Chrome 종료 후 다음 작업 대기

## 2. 권장 환경

- Windows 10/11
- Python 3.11.x
- Chrome 설치
- MariaDB 접속 가능 환경
- PowerShell

Python 3.13보다 프로젝트에서 확인된 Python 3.11 환경 사용을 권장한다.

## 3. 주요 파일

```text
crawl-deamon/
├─ main.py
├─ .env
├─ requirements.txt
├─ exe.md
├─ history.md
├─ logs/
└─ src/
   ├─ database/
   │  ├─ db_connection.py
   │  └─ execution_repository.py
   ├─ utils/
   │  ├─ daemon_logger.py
   │  └─ selenium_utils.py
   └─ workers/
      └─ panda_worker.py
```

## 4. 환경변수 설정

프로젝트 최상위에 `.env` 파일을 둔다.

```env
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=사용자명
DB_PASSWORD=비밀번호
DB_NAME=데이터베이스명
DB_CHARSET=utf8mb4
DB_CONNECT_TIMEOUT=10
```

필수값:

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`

## 5. 개발 환경 실행

### 가상환경 활성화

```powershell
cd E:\git\crawl-deamon
.\venv\Scripts\Activate.ps1
python --version
```

### 패키지 설치

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

PyInstaller가 설치되지 않은 경우:

```powershell
pip install pyinstaller
```

### DB 연결만 확인

```powershell
python .\src\database\db_connection.py
```

정상 결과:

```text
[DB] MariaDB 연결 성공
[DB] SELECT 1 실행 성공
```

### 데몬 실행

```powershell
python main.py
```

정상 실행 흐름:

```text
MariaDB 연결
→ READY 작업 대기
→ 작업이 있으면 Chrome 실행
→ 팬더라이브 자동 로그인
→ 랭킹 조회 및 메시지 발송
→ Chrome 종료
→ 다음 READY 작업 대기
```

종료:

```text
Ctrl+C
```

## 6. 실행 설정

작업의 `SETTING_JSON`에는 다음 값이 필요하다.

```json
{
  "ranking_type": "rankingNewBJ",
  "start_page": 1,
  "end_page": 5,
  "message_text": "발송할 메시지",
  "exclude_duplicate_yn": true,
  "login_id": "팬더라이브 아이디",
  "login_password": "팬더라이브 비밀번호"
}
```

지원 랭킹:

- `rankingPersonalBJ`
- `rankingNewBJ`
- `rankingCrewBJ`
- `rankingPopular`

`fields` 배열 형식의 설정 JSON도 지원한다.

## 7. EXE 빌드

### 기존 빌드 결과 삭제

```powershell
cd E:\git\crawl-deamon
.\venv\Scripts\Activate.ps1

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force crawl-deamon.spec -ErrorAction SilentlyContinue
```

### PyInstaller 빌드

```powershell
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
--hidden-import selenium `
--hidden-import selenium.webdriver `
--hidden-import selenium.webdriver.common.by `
main.py
```

완성 경로:

```text
E:\git\crawl-deamon\dist\crawl-deamon\
```

## 8. 빌드 결과에 `.env` 복사

```powershell
Copy-Item `
E:\git\crawl-deamon\.env `
E:\git\crawl-deamon\dist\crawl-deamon\.env `
-Force
```

최종 구조:

```text
dist\crawl-deamon\
├─ crawl-deamon.exe
├─ .env
├─ Python 관련 파일
├─ Selenium 관련 파일
└─ undetected_chromedriver 관련 파일
```

## 9. EXE 실행

```powershell
cd E:\git\crawl-deamon\dist\crawl-deamon
.\crawl-deamon.exe
```

`crawl-deamon.exe`와 `.env`는 같은 폴더에 있어야 한다.

## 10. 실행 배치파일

`dist\crawl-deamon\start.bat`:

```bat
@echo off
cd /d "%~dp0"
crawl-deamon.exe
pause
```

실행:

```powershell
.\start.bat
```

## 11. 배포 폴더 복사

```powershell
Copy-Item `
"E:\git\crawl-deamon\dist\crawl-deamon" `
"C:\Users\772vj\Desktop\crawl-deamon" `
-Recurse `
-Force
```

기존 대상 폴더를 완전히 교체하려면 먼저 삭제한 후 복사한다.

```powershell
Remove-Item `
"C:\Users\772vj\Desktop\crawl-deamon" `
-Recurse `
-Force `
-ErrorAction SilentlyContinue

Copy-Item `
"E:\git\crawl-deamon\dist\crawl-deamon" `
"C:\Users\772vj\Desktop\crawl-deamon" `
-Recurse
```

## 12. 로그

로그 경로:

```text
logs\crawl-daemon-YYYY-MM-DD.log
```

동작:

- 콘솔과 파일에 동시에 기록
- 날짜가 바뀌면 새 로그 파일 생성
- 데몬 시작 시 오래된 로그 정리
- 매일 자정이 지난 뒤 오래된 로그 정리
- 30일이 지난 로그 파일 삭제
- READY 작업이 없으면 5분마다 정상 작동 로그 기록

## 13. 주요 실행 기준

- READY 작업 조회: 5초 간격
- DB 재연결: 10초 간격
- 정상 작동 로그: 5분 간격
- 메시지 발송 간격: 5~7초 랜덤
- 로그 보관기간: 30일
- 작업마다 새 Chrome 실행
- 작업 종료 시 Chrome 자동 종료
- 메시지는 `message_text` 값을 그대로 발송

## 14. 문제 확인

### `No module named PyInstaller`

```powershell
pip install pyinstaller
python -m PyInstaller --version
```

### `No module named selenium`

현재 활성화된 가상환경에 설치한다.

```powershell
python -m pip install selenium undetected-chromedriver
python -c "import selenium; import undetected_chromedriver; print('정상')"
```

### DB 연결 실패

확인 항목:

- `.env` 위치와 값
- MariaDB 서버 실행 여부
- 방화벽과 포트
- DB 사용자 권한
- DB 이름

### 로그인 실패

확인 항목:

- `SETTING_JSON`의 `login_id`
- `SETTING_JSON`의 `login_password`
- 팬더라이브 로그인 화면 변경 여부
- 광고 팝업 및 로그인 버튼 구조 변경 여부

### 로그 확인

```powershell
Get-Content `
.\logs\crawl-daemon-$(Get-Date -Format yyyy-MM-dd).log `
-Wait
```
