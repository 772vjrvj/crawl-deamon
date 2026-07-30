## 개발 진행 상황

### 1. MariaDB 연결 준비

- `PyMySQL` 설치
- `python-dotenv` 설치
- 패키지 import 확인 완료

```powershell
python -c "import pymysql; from dotenv import load_dotenv; print('DB 패키지 설치 완료')"


### 2. DB 환경변수 구성

- 프로젝트 최상위에 `.env` 생성
- MariaDB 접속정보를 소스코드와 분리
- `.gitignore`에 `.env` 추가