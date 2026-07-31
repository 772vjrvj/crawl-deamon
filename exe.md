cd E:\git\crawl-deamon

.\venv\Scripts\Activate.ps1

python --version


PyInstaller 설치:
pip install pyinstaller


기존 빌드 결과 삭제:
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force crawl-deamon.spec -ErrorAction SilentlyContinue


빌드
pyinstaller `
--noconfirm `
--clean `
--onedir `
--contents-directory . `
--console `
--name crawl-deamon `
--paths . `
--collect-all selenium `
--collect-all undetected_chromedriver `
main.py


완성 경로:
E:\git\crawl-deamon\dist\crawl-deamon\

dist\crawl-deamon\
├─ crawl-deamon.exe
├─ python 관련 DLL
├─ selenium 관련 파일
└─ undetected_chromedriver 관련 파일


2. .env 복사
Copy-Item `
E:\git\crawl-deamon\.env `
E:\git\crawl-deamon\dist\crawl-deamon\.env


dist\crawl-deamon\
├─ crawl-deamon.exe
└─ .env


3. 빌드 후 실행
cd E:\git\crawl-deamon\dist\crawl-deamon
.\crawl-deamon.exe


Chrome 실행
→ 팬더라이브 로그인
→ 콘솔 Enter
→ DB 작업 대기
→ 예약 작업 실행




4. 실행 배치파일
E:\git\crawl-deamon\dist\crawl-deamon\start.bat

@echo off
cd /d "%~dp0"
crawl-deamon.exe
pause




다시 빌드할 때

cd E:\git\crawl-deamon
.\venv\Scripts\Activate.ps1

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


Copy-Item .env .\dist\crawl-deamon\.env -Force

Copy-Item `
"E:\git\crawl-deamon\dist\crawl-deamon" `
"C:\Users\772vj\Desktop\crawl-deamon" `
-Recurse



cd .\dist\crawl-deamon
.\crawl-deamon.exe

