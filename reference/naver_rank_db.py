import requests

BASE_URL = "https://goodbye772.com"
USER_SERVICE_ID = 8

API_KEY = "rank_4c51b5e5df18d1d99cfc2d64fe6b6f178dd25be138dd5718af13e0c0b7649186"

headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
}

items = [
    {
        "keyword": "악세서리",
        "mid": "91188328209",
    },
    {
        "keyword": "여성가방",
        "mid": "48982143201",
    },
    {
        "keyword": "휴대폰악세사리",
        "mid": "91297922991",
    },
    {
        "keyword": "청바지",
        "mid": "58906392124",
    },
    {
        "keyword": "초코릿",
        "mid": "53230785887",
    }
    # 여기에 최대 5,000개까지 추가 하지만 가급적 갯수를 제한해 주세요
]






if len(items) > 5000:
    raise ValueError("한 번에 최대 5,000건까지 등록할 수 있습니다.")

payload = {
    "requestKey": "python-test-001",
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
    timeout=60,
)

print("status =", response.status_code)
print("body =", response.text)

response.raise_for_status()

result = response.json()

print("workerId =", result["workerId"])
print("등록 건수 =", result["totalCount"])