import json
import requests
from pathlib import Path


BASE_URL = "https://goodbye772.com"
USER_SERVICE_ID = 8
API_KEY = "rank_4c51b5e5df18d1d99cfc2d64fe6b6f178dd25be138dd5718af13e0c0b7649186"

BASE_DIR = Path(__file__).resolve().parent
JSON_FILE = BASE_DIR / "reference" / "rank_items.json"

print("JSON 경로 =", JSON_FILE)
print("파일 존재 여부 =", JSON_FILE.exists())

if not JSON_FILE.exists():
    raise FileNotFoundError(
        f"JSON 파일을 찾을 수 없습니다: {JSON_FILE}"
    )

headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
}

with JSON_FILE.open("r", encoding="utf-8") as file:
    items = json.load(file)

if not isinstance(items, list):
    raise ValueError("JSON 파일의 최상위 구조는 배열이어야 합니다.")

if not items:
    raise ValueError("등록할 항목이 없습니다.")

if len(items) > 5000:
    raise ValueError("한 번에 최대 5,000건까지 등록할 수 있습니다.")

for index, item in enumerate(items, start=1):
    if not item.get("keyword"):
        raise ValueError(f"{index}번째 항목의 keyword가 없습니다.")

    if not item.get("mid"):
        raise ValueError(f"{index}번째 항목의 mid가 없습니다.")

payload = {
    "requestKey": "python-json-test-001",
    "items": items,
}

url = (
    f"{BASE_URL}/api/my-services/"
    f"{USER_SERVICE_ID}/rank-requests"
)

response = requests.post(
    url,
    headers=headers,
    json=payload,
    timeout=120,
)

print("status =", response.status_code)
print("body =", response.text)

response.raise_for_status()

result = response.json()

print("workerId =", result["workerId"])
print("등록 건수 =", result["totalCount"])