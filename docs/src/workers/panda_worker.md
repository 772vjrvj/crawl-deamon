# PANDA_MESSAGE 빌드 및 운영 가이드

## 1. 개요

`PANDA_MESSAGE` 데몬은 PandaLive 랭킹 데이터를 조회한 뒤 대상자에게 메시지를 발송하는 전용 데몬이다.

현재 최종 구조는 다음과 같다.

- Selenium + undetected-chromedriver 사용
- 로그인 계정은 `login_accounts` 배열의 첫 번째 계정만 사용
- 로그인 실패 시 최초 1회 + 재시도 1회, 최대 2회 시도
- 랭킹 데이터를 먼저 조회
- 모든 발송 대상을 `SERVICE_EXECUTION_RESULT`에 선 INSERT
- 메시지 발송 성공 시 `SUCCESS`
- 상대방 수신 제한 시 해당 대상 `FAIL` 후 다음 대상 진행
- 발신 계정 전송 제한 시 10분 쿨타임
- 10분 후 동일 계정 / 동일 대상 1회 재시도
- 재시도 후에도 발신 제한이면 현재 대상 `FAIL`
- 남아 있는 `READY` 대상은 `SKIP` 후 현재 작업 종료
- 일반 메시지 발송 간격은 5초
- Proxy / Bright Data는 최종 구조에서 사용하지 않음

---

## 2. 준비 사항

권장 환경:

- Windows 10 / 11 64bit
- Python 3.11 64bit
- Google Chrome 설치
- 최신 통합 데몬 소스 사용
- 기존에 실행 중인 구버전 PANDA_MESSAGE 데몬 종료

구버전 데몬이 동시에 실행되지 않도록 주의한다.

특히 동일한 `DAEMON_SERVICE_CODE=PANDA_MESSAGE`를 사용하는 EXE가 여러 개 실행되면 동일한 READY 작업을 대상으로 경쟁할 수 있으므로 운영 PC에서는 PANDA_MESSAGE 데몬을 하나만 실행하는 것을 권장한다.

---

## 3. PANDA_MESSAGE 전용 가상환경과 패키지 설치

PowerShell에서 프로젝트 루트로 이동한다.

```powershell
cd E:\git\crawl-deamon
```

Python 3.11 가상환경 생성:

```powershell
py -3.11 -m venv .venv-panda
```

가상환경 활성화:

```powershell
.\.venv-panda\Scripts\Activate.ps1
```

pip 업데이트:

```powershell
python -m pip install --upgrade pip
```

PANDA_MESSAGE 전용 패키지 설치:

```powershell
python -m pip install -r .\requirements-panda.txt
```

설치 확인:

```powershell
python -c "import pymysql, selenium, undetected_chromedriver; print('PANDA 패키지 확인 완료')"
python -m PyInstaller --version
```

---

## 4. PANDA_MESSAGE EXE 빌드

프로젝트 루트에서 실행한다.

```powershell
cd E:\git\crawl-deamon
.\.venv-panda\Scripts\Activate.ps1
```

이전 빌드 파일 제거:

```powershell
Remove-Item -Recurse -Force .\build\crawl-deamon-panda -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .\dist\crawl-deamon-panda -ErrorAction SilentlyContinue
Remove-Item -Force .\crawl-deamon-panda.spec -ErrorAction SilentlyContinue
```

빌드:

```powershell
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --contents-directory _internal `
  --console `
  --name crawl-deamon-panda `
  --paths "E:\git\crawl-deamon" `
  --collect-all selenium `
  --collect-all undetected_chromedriver `
  --hidden-import src.workers.panda_worker `
  --exclude-module whisper `
  --exclude-module torch `
  --exclude-module pyaudiowpatch `
  --exclude-module pyautogui `
  --exclude-module pyperclip `
  --exclude-module src.core.services.ai_whisper `
  --exclude-module src.workers.naver_shop_total_detail_worker `
  .\main.py
```

빌드 완료 위치:

```text
E:\git\crawl-deamon\dist\crawl-deamon-panda\
└─ crawl-deamon-panda.exe
```

PANDA_MESSAGE 빌드에는 Whisper, 오디오 캡처, PyAutoGUI, ffmpeg가 필요하지 않다.

---

## 5. PANDA_MESSAGE용 .env 설정

빌드 결과 폴더의 EXE와 같은 위치에 `.env`를 생성한다.

```env
DB_HOST=서버주소
DB_PORT=3306
DB_NAME=DB명
DB_USER=계정
DB_PASSWORD=비밀번호
DB_CHARSET=utf8mb4
DB_CONNECT_TIMEOUT=10

DAEMON_SERVICE_CODE=PANDA_MESSAGE
SELENIUM_HEADLESS=true
```

파일 위치:

```text
dist\crawl-deamon-panda\
├─ crawl-deamon-panda.exe
└─ .env
```

### SELENIUM_HEADLESS

운영:

```env
SELENIUM_HEADLESS=true
```

점검:

```env
SELENIUM_HEADLESS=false
```

- `true`: Chrome 창을 표시하지 않는 headless 실행
- `false`: 점검 시 실제 Chrome 창 표시

현재 `SeleniumUtils`에서는 headless가 활성화되면 Chrome 옵션에 `--headless=new`를 추가한다.

단, `undetected_chromedriver`의 초기 기동 또는 드라이버 생성 재시도 과정에서 Chrome 창이 순간적으로 나타났다 사라질 수 있다.

현재 `SeleniumUtils`는 실행마다 별도의 임시 Chrome 프로필을 생성하여 `--user-data-dir`로 사용한다. 따라서 일반 Chrome 또는 별도의 브라우저 매크로와 프로필 lock이 직접 충돌할 가능성은 낮다.

또한 Chrome 종료 시 전체 `chrome.exe`를 종료하는 것이 아니라 데몬이 생성한 임시 프로필을 사용하는 Chrome 프로세스만 정리하도록 되어 있다.

---

## 6. 실행

빌드 폴더로 이동:

```powershell
cd E:\git\crawl-deamon\dist\crawl-deamon-panda
```

실행:

```powershell
.\crawl-deamon-panda.exe
```

정상적인 초기 로그 예:

```text
[DAEMON] 담당 SERVICE_CODE=PANDA_MESSAGE
[SELENIUM] HEADLESS=true
[DB] MariaDB 연결 성공
[QUEUE] READY 작업 없음 | 5초 후 재조회
```

READY 작업이 존재하면 PandaLive 작업을 시작한다.

---

## 7. Worker 전체 실행 흐름

PANDA_MESSAGE Worker의 실질적인 작업 시작 함수는 다음이다.

```python
execute_message_job(...)
```

전체 흐름:

```text
main.py
  ↓
READY 작업 조회
  ↓
PANDA_MESSAGE 작업 선점
  ↓
execute_message_job(...)
  ↓
login_accounts[0] 선택
  ↓
PandaLive 로그인
  ↓
랭킹 페이지 이동
  ↓
랭킹 API 조회
  ↓
순위 범위 필터
  ↓
중복 제거
  ↓
SERVICE_EXECUTION_RESULT 전체 선 INSERT
  ↓
메시지 발송 시작
```

코드 전체를 다시 볼 때는 다음 순서로 보면 된다.

```text
main.py
→ execute_message_job()
→ login_panda_with_retry()
→ login_panda()
→ fetch_rankings()
→ insert_all_results_first()
→ send_messages_after_insert()
```

---

## 8. 로그인 처리

현재 로그인은 최대 2회 시도한다.

```text
1차 로그인 시도
  ├─ 성공 → 작업 계속
  └─ 실패
       ↓
     1초 대기
       ↓
     2차 로그인 시도
       ├─ 성공 → 작업 계속
       └─ 실패 → 최종 RuntimeError
```

즉 최초 로그인 1회, 실패하면 딱 1회 추가 재시도하여 총 최대 2회다.

대표 로그:

```text
[LOGIN] 팬더 로그인 시작 | attempt=1/2
[LOGIN] 팬더 로그인 실패 | attempt=1/2
[LOGIN] 로그인 전체 과정을 1회 다시 시도합니다.
[LOGIN] 팬더 로그인 시작 | attempt=2/2
```

---

## 9. 랭킹 조회 및 결과 선 INSERT

랭킹은 페이지 단위로 조회한다. 기본 페이지 크기는 20건이다.

예:

```text
[RANKING] page=1 | 조회=20
[RANKING] page=2 | 조회=20
[RANKING] page=3 | 조회=20
```

설정된 `start_rank ~ end_rank` 범위를 기준으로 최종 발송 대상을 필터링한다.

예:

```text
[RANKING] 전체조회=100 | 순위범위=50~100 | 순위필터후=51 | 최종대상=51
```

실제 메시지 발송 전에 모든 대상은 `SERVICE_EXECUTION_RESULT`에 먼저 INSERT한다.

- `userIdx` 존재 → `READY`
- `userIdx` 없음 → `SKIP`

---

## 10. 메시지 발송 기본 처리

현재 로그인 계정은 `login_accounts` 배열의 첫 번째 계정만 사용한다.

```python
account = accounts[0]
```

다중 계정 라운드로빈 방식은 사용하지 않는다.

일반 발송 간격:

```text
5초
```

정상 흐름:

```text
발송 시도
↓
SUCCESS
↓
5초 대기
↓
다음 대상
```

대표 로그:

```text
[MESSAGE] 발송 시도
[MESSAGE] 발송 성공
[MESSAGE] 5초 후 다음 대상으로 진행합니다.
```

---

## 11. 상대 BJ 쪽지 수신 제한

확인된 메시지 예:

```text
BJ님께서 브론즈(30일) 등급 아래인 경우 쪽지 수신을 제한하여 발송이 불가능합니다.
```

판별 문자열:

```text
쪽지 수신을 제한
```

처리:

```text
현재 대상 FAIL
↓
현재 로그인 계정 유지
↓
5초 후 다음 대상
```

---

## 12. 발신 계정 쪽지 전송 제한

확인된 서버 메시지:

```text
쪽지 전송이 제한되었습니다. 잠시 후 다시 시도해 주세요.
```

판별 문자열:

```text
쪽지 전송이 제한되었습니다
```

현재 최종 처리:

```text
전송 제한 발생
↓
600초(10분) 쿨타임
↓
동일 계정
↓
동일 대상 1회 재시도
```

현재 운영값:

```python
MESSAGE_LIMIT_COOLDOWN_SEC = 10 * 60
```

실제 테스트에서 10분 후 동일 대상 재발송 성공을 반복 확인하였다.

```text
18:26:22 전송 제한
18:36:22 쿨타임 종료
18:36:23 동일 대상 발송 성공

18:36:48 전송 제한
18:46:48 쿨타임 종료
18:46:49 동일 대상 발송 성공
```

현재는 안정성을 우선해 10분을 운영 기준값으로 사용한다.

단, PandaLive의 공식 제한 시간이 정확히 10분이라고 확인된 것은 아니며 현재 실제 테스트 결과를 기준으로 잡은 값이다.

---

## 13. 10분 후에도 전송 제한이 계속되는 경우

10분 대기 후 동일 대상 재시도에서도 다시 전송 제한이 발생하면:

```text
현재 대상 FAIL
↓
stopped_early=True
↓
남은 READY 대상 SKIP
↓
현재 HIST 작업 종료
```

남은 대상을 계속 발송하지 않는다.

---

## 14. 성공 / 실패 로그

성공:

```text
[MESSAGE] 발송 성공 | HIST_ID=... | n/total | login_id=... | userNick=... | userIdx=...
```

실패:

```text
[MESSAGE] 발송 실패 | HIST_ID=... | n/total | login_id=... | userNick=... | userIdx=... | ERROR=...
```

쿨타임:

```text
[MESSAGE] 전송 제한 발생
[MESSAGE] 쿨타임 시작
[MESSAGE] 쿨타임 종료
```

문제 발생 시에는 해당 `HIST_ID` 기준으로 앞뒤 로그를 함께 확인한다.

---

## 15. 현재 운영 테스트 결과

현재 테스트에서 다음 흐름이 반복 확인되었다.

```text
여러 건 정상 발송
↓
발신 계정 전송 제한
↓
10분 대기
↓
동일 대상 재시도 성공
↓
다음 대상 발송 계속
```

현재 테스트에서는 약 5건 단위의 성공 흐름이 반복적으로 관찰되었으나, 이를 PandaLive의 공식 고정 제한 수치로 단정하지 않는다.

현재 코드는 서버에서 실제 전송 제한 응답이 발생했을 때만 10분 쿨타임을 적용한다.

---

## 16. Bright Data Proxy 검토 이력

초기에는 PandaLive 메시지 전송 제한 발생 시 Bright Data Proxy 적용을 검토하였다.

검토 대상:

- Bright Data Residential Proxy
- Bright Data ISP Proxy
- requests + Selenium 쿠키 연동
- Exit IP 변경 확인

일부 테스트에서는 Bright Data Proxy 자체 연결과 Exit IP 변경이 정상적으로 확인되었다.

```text
데몬
↓
Bright Data Proxy
↓
외부 인터넷
```

그러나 PandaLive 메시지 API에서는 Exit IP가 변경된 상태에서도:

```text
쪽지 전송이 제한되었습니다
```

응답이 계속 발생하였다.

따라서 PandaLive의 메시지 발송 제한이 단순 IP 기준만으로 판단된다고 볼 수 없었다.

최종적으로 Proxy 방식은 제거하였다.

---

## 17. Bright Data Residential / KYC 참고

Bright Data의 Residential Network는 접근 정책이 강화되어 있으며, 사용 범위에 따라 Compliance / KYC 절차가 요구될 수 있다.

KYC:

```text
Know Your Customer
```

Residential Network에는 제한적인 즉시 접근 방식과 KYC가 필요한 Full Access가 구분될 수 있다.

Full Residential Access 또는 추가 권한 사용 시 일반적으로 다음과 같은 검증이 요구될 수 있다.

```text
회사 / 사업자 정보
회사 이메일
서비스 사용 목적
회사 등록 관련 서류
```

추가 확인이 필요한 경우 신분증이나 영상 통화 등 추가 검증이 요청될 수 있다.

중요:

- Residential 전체가 무조건 KYC 필수라는 의미는 아님
- 제한적인 Immediate Access가 존재할 수 있음
- Full Access는 등록 사업자 / 회사 중심의 검증 절차가 적용될 수 있음
- 개인 또는 단순 개인 프로젝트는 Full Residential Access 승인 대상으로 가정하지 않는 것이 좋음

현재 PANDA_MESSAGE에서는 이 절차까지 진행하지 않고 Bright Data를 최종 제거했다.

---

## 18. Proxy를 최종 제거한 이유

최종적으로 Proxy를 제거한 이유:

```text
1. Bright Data Exit IP 변경 자체는 확인됨
2. IP 변경 후에도 PandaLive 전송 제한이 계속 발생
3. PandaLive 제한이 단순 IP 기준으로 보이지 않음
4. Bright Data Residential Full Access 사용 시 KYC / 사업자 검증 절차가 복잡해짐
5. Proxy 없이 실제 제한 응답을 기준으로 쿨타임 후 재시도하는 구조가 더 단순했음
```

따라서 현재 최종 PANDA_MESSAGE 데몬에서는 Bright Data 관련 코드를 사용하지 않는다.

---

## 19. Selenium Headless 및 다른 Chrome 매크로와 동시 사용

현재 `SeleniumUtils`는 실행마다 UUID 기반 임시 profile을 만든다.

예:

```text
%TEMP%\selenium_profiles\profile_<UUID>
```

해당 profile은 Chrome 실행 시 `--user-data-dir`로 지정된다.

따라서 다른 일반 Chrome 또는 다른 매크로 Chrome과 profile을 공유하지 않는다.

Chrome 정리 또한 해당 임시 profile을 사용하는 `chrome.exe`만 대상으로 한다.

따라서 다른 Chrome 브라우저가 같이 실행 중이라는 이유만으로 직접 충돌할 가능성은 낮다.

주의할 부분은 브라우저 자체보다는 다음이다.

```text
같은 사이트
같은 로그인 계정
같은 작업 대상
같은 파일 / 다운로드 경로
PC 자원 부족
```

특히 같은 Panda 계정을 서로 다른 자동화 프로그램에서 동시에 사용하는 상황은 피하는 것이 좋다.

---

## 20. Headless인데 순간적으로 Chrome 창이 나타나는 경우

`SELENIUM_HEADLESS=true`여도 프로그램 시작 또는 드라이버 재생성 과정에서 Chrome 창이 순간적으로 나타났다 사라질 수 있다.

현재 `SeleniumUtils`의 드라이버 생성은 다음과 같은 재시도 구조를 가진다.

```text
감지된 Chrome major로 실행
↓ 실패 시
오류 메시지에서 추출한 major로 실행
↓ 실패 시
version_main 없이 UC 자동 실행
↓ 실패 시
추출한 major로 최종 재시도
```

`uc.Chrome()` 생성 실패 중 Chrome 프로세스만 먼저 생성되고 WebDriver 객체 생성은 실패하는 경우가 있을 수 있다.

현재 코드는 이런 잔여 Chrome을 임시 profile 기준으로 정리하도록 되어 있다.

따라서:

```text
처음에 Chrome 창이 잠깐 나타남
↓
곧 사라짐
↓
이후 headless 작업 정상 진행
```

이라면 큰 문제로 보지 않는다.

반대로 Chrome 창이 계속 유지되면서 실제 작업까지 진행된다면 `.env`와 `SeleniumUtils(headless=True)` 전달 여부를 확인한다.

---

## 21. 배포

다른 PC에 옮길 때는 `crawl-deamon-panda.exe` 하나만 복사하지 않는다.

반드시 다음 폴더 전체를 복사한다.

```text
dist\crawl-deamon-panda\
```

예:

```text
crawl-deamon-panda\
├─ crawl-deamon-panda.exe
├─ .env
├─ PyInstaller 의존 파일
└─ 기타 빌드 결과 파일
```

현재 빌드는 `--onedir` 방식이므로 EXE 단독 실행을 전제로 하지 않는다.

PANDA_MESSAGE 빌드에는 `resources\...\ffmpeg.exe`가 필요하지 않다.

---

## 22. 장애 발생 시 확인 순서

```text
1. DAEMON_SERVICE_CODE=PANDA_MESSAGE 확인
2. DB 연결 확인
3. SERVICE_EXECUTION_HIST의 READY 작업 존재 여부 확인
4. 동일 PANDA_MESSAGE 데몬이 중복 실행 중인지 확인
5. login_accounts 배열 확인
   - 현재는 첫 번째 계정만 사용
6. 로그인 로그 확인
   - attempt=1/2
   - attempt=2/2
7. 랭킹 조회 로그 확인
8. SERVICE_EXECUTION_RESULT 선 INSERT 확인
9. 메시지 발송 로그 확인
   - 발송 시도
   - 발송 성공
   - 발송 실패
10. 상대방 수신 제한 여부 확인
    - "쪽지 수신을 제한"
11. 발신 계정 제한 여부 확인
    - "쪽지 전송이 제한되었습니다"
12. 발신 제한인 경우 확인
    - 600초 쿨타임 시작
    - 600초 후 쿨타임 종료
    - 동일 대상 재시도
13. 10분 후에도 제한이면 확인
    - 현재 대상 FAIL
    - 남은 READY SKIP
    - 작업 조기 종료
14. Selenium 관련 문제면 확인
    - SELENIUM_HEADLESS 값
    - Chrome 설치 여부
    - Chrome major 버전
    - undetected_chromedriver
    - 임시 profile 잔여 여부
```

운영 중 문제가 발생하면 바로 코드를 수정하지 말고 해당 `HIST_ID`의 전체 로그를 먼저 보관하고 원인을 확인한다.

---

## 23. 현재 최종 운영 기준 요약

```text
SERVICE_CODE
PANDA_MESSAGE

로그인 계정
login_accounts[0] 하나만 사용

로그인
실패 시 1회 추가 재시도
총 최대 2회

메시지 간격
5초

상대 BJ 수신 제한
현재 대상 FAIL
다음 대상 계속

발신 계정 전송 제한
10분 대기
동일 계정 / 동일 대상 1회 재시도

10분 후에도 제한
현재 대상 FAIL
남은 READY SKIP
현재 HIST 작업 종료

Proxy
사용하지 않음

Bright Data
최종 코드에서 제거

Selenium
undetected-chromedriver

운영 Headless
SELENIUM_HEADLESS=true
```

현재 버전을 기준 버전으로 프리징하고, 이후 문제가 발생하면 해당 시점 로그를 기준으로 추가 수정한다.
