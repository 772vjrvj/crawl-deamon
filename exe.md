실행 배치파일
E:\git\crawl-deamon\dist\crawl-deamon\start.bat

@echo off
cd /d "%~dp0"
crawl-deamon.exe
pause



cd E:\git\crawl-deamon
.\venv\Scripts\Activate.ps1


기존 빌드 결과 삭제:
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

