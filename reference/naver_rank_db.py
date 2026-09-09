import requests

BASE_URL = "https://goodbye772.com/"

USER_SERVICE_ID = 8

API_KEY = (
    "rank_4c51b5e5df18d1d99cfc2d64fe6b6f178dd25be138dd5718af13e0c0b7649186"
)

headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
}

payload = {
    "requestKey": "python-test-001",
    "items": [
        {
            "keyword": "악세서리",
            "mid": "91188328209",
        },
        {
            "keyword": "여성가방",
            "mid": "48982143201",
        },
    ],
}

url = (
    f"{BASE_URL}/api/my-services/"
    f"{USER_SERVICE_ID}/rank-requests"
)

response = requests.post(
    url,
    headers=headers,
    json=payload,
    timeout=30,
)

print("status =", response.status_code)
print("body =", response.text)

response.raise_for_status()

result = response.json()
worker_id = result["workerId"]

print("workerId =", worker_id)