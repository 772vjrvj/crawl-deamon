import json
import time
import requests


BASE_URL = "https://goodbye772.com"
USER_SERVICE_ID = 8
WORKER_ID = 163

API_KEY = "rank_4c51b5e5df18d1d99cfc2d64fe6b6f178dd25be138dd5718af13e0c0b7649186"

headers = {
    "X-API-Key": API_KEY,
    "Accept": "application/json",
}


def get_progress():
    url = (
        f"{BASE_URL}/api/my-services/"
        f"{USER_SERVICE_ID}/rank-requests/"
        f"{WORKER_ID}"
    )

    response = requests.get(
        url,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_all_items():
    all_items = []
    page = 0
    page_size = 100

    while True:
        url = (
            f"{BASE_URL}/api/my-services/"
            f"{USER_SERVICE_ID}/rank-requests/"
            f"{WORKER_ID}/items"
        )

        response = requests.get(
            url,
            headers=headers,
            params={
                "page": page,
                "size": page_size,
            },
            timeout=60,
        )

        response.raise_for_status()

        result = response.json()

        items = result.get("items", [])
        all_items.extend(items)

        total_elements = result.get("totalElements", 0)
        total_pages = result.get("totalPages", 0)
        is_last = result.get("last", False)

        print(
            f"{page + 1}페이지 조회 완료 | "
            f"이번 페이지={len(items)}개 | "
            f"누적={len(all_items)}개 | "
            f"전체={total_elements}개"
        )

        if is_last or page + 1 >= total_pages or not items:
            break

        page += 1
        time.sleep(0.2)

    return all_items


if __name__ == "__main__":
    progress = get_progress()

    print("현재 진행률")
    print(json.dumps(progress, ensure_ascii=False, indent=2))

    items = get_all_items()

    with open(
            "rank_results.json",
            "w",
            encoding="utf-8",
    ) as file:
        json.dump(
            items,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\n전체 결과 저장 완료: {len(items)}개")
    print("저장 파일: rank_results.json")