# -*- coding: utf-8 -*-
"""
MariaDB 연결 관리

- 프로젝트 최상위의 .env 파일에서 DB 접속정보를 읽는다.
- create_connection()을 호출하면 MariaDB 연결 객체를 반환한다.
- 이 파일을 직접 실행하면 SELECT 1로 연결 상태를 확인한다.
"""

import os
from pathlib import Path
from typing import Optional

import pymysql
from pymysql.connections import Connection
from dotenv import load_dotenv


# 현재 파일 위치:
# E:\git\crawl-deamon\src\database\db_connection.py
#
# 프로젝트 최상위:
# E:\git\crawl-deamon
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 프로젝트 최상위의 .env 파일을 읽는다.
load_dotenv(PROJECT_ROOT / ".env")


def get_required_env(name: str) -> str:
    """
    필수 환경변수를 가져온다.

    값이 없으면 프로그램을 잘못 실행하지 않도록 즉시 오류를 발생시킨다.
    """
    value = os.getenv(name)

    if value is None or not value.strip():
        raise RuntimeError(f".env에 {name} 값이 없습니다.")

    return value.strip()


def create_connection() -> Connection:
    """
    MariaDB에 연결하고 연결 객체를 반환한다.

    Returns:
        PyMySQL Connection 객체
    """
    return pymysql.connect(
        host=get_required_env("DB_HOST"),
        port=int(get_required_env("DB_PORT")),
        user=get_required_env("DB_USER"),
        password=get_required_env("DB_PASSWORD"),
        database=get_required_env("DB_NAME"),
        charset=os.getenv("DB_CHARSET", "utf8mb4"),
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def test_connection() -> None:
    """
    MariaDB 연결 후 SELECT 1을 실행하여 연결 상태를 확인한다.
    """
    connection: Optional[Connection] = None

    try:
        print("[DB] MariaDB 연결을 시작합니다.")

        connection = create_connection()

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS result")
            row = cursor.fetchone()

        if row and row.get("result") == 1:
            print("[DB] MariaDB 연결 성공")
            print("[DB] SELECT 1 실행 성공")
        else:
            raise RuntimeError(f"SELECT 1 결과가 올바르지 않습니다: {row}")

    except Exception as error:
        print(f"[DB] MariaDB 연결 실패: {error}")
        raise

    finally:
        if connection:
            connection.close()
            print("[DB] MariaDB 연결을 종료했습니다.")


if __name__ == "__main__":
    test_connection()