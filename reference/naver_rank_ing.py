import requests

# BASE_URL = "http://localhost:81"
BASE_URL = "https://goodbye772.com/"

USER_SERVICE_ID = 8
WORKER_ID = 163

API_KEY = "rank_4c51b5e5df18d1d99cfc2d64fe6b6f178dd25be138dd5718af13e0c0b7649186"

headers = {
    "X-API-Key": API_KEY,
}

url = (
    f"{BASE_URL}/api/my-services/"
    f"{USER_SERVICE_ID}/rank-requests/{WORKER_ID}"
)

response = requests.get(
    url,
    headers=headers,
    timeout=30,
)

print("status =", response.status_code)
print("body =", response.text)

response.raise_for_status()
progress = response.json()

print("진행 상태 =", progress)