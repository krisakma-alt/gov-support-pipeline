"""
카카오 로컬 API 기반 신규 오픈 매장 수집 스크립트
- 검색 지역: 합정동, 망원동, 서교동(홍대)
- 결과 저장: new_stores.xlsx
"""

import requests
import pandas as pd
import time
from openpyxl.utils import get_column_letter

API_KEY = "1a3838948de7969e0c8542b2dc8fdb55"
HEADERS = {"Authorization": f"KakaoAK {API_KEY}"}

# 검색 지역 (경도 x, 위도 y)
AREAS = [
    {"name": "합정동",     "x": "126.9146", "y": "37.5494"},
    {"name": "망원동",     "x": "126.9065", "y": "37.5567"},
    {"name": "서교동(홍대)", "x": "126.9230", "y": "37.5551"},
]

# 카카오 공식 카테고리 코드
CATEGORY_CODES = {
    "FD6": "음식점",
    "CE7": "카페",
    "CS2": "편의점",
}

# 키워드 검색 카테고리 (공식 코드 없음)
SEARCH_KEYWORDS = [
    "베이커리", "술집", "바", "치킨", "피자", "패스트푸드",
    "분식", "족발", "보쌈", "고기구이", "해산물",
    "중식당", "일식당", "양식당",
    "미용실", "네일샵", "피부관리", "마사지", "스파",
    "세탁소", "꽃집", "문구점", "서점",
    "헬스장", "필라테스", "요가", "볼링장", "노래방", "당구장",
    "동물병원", "펫샵",
]


def search_by_category(area: dict, code: str, name: str) -> list:
    """카테고리 코드로 장소 검색 (최대 45개)"""
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    results = []

    for page in range(1, 4):
        params = {
            "category_group_code": code,
            "x": area["x"],
            "y": area["y"],
            "radius": 800,
            "page": page,
            "size": 15,
            "sort": "accuracy",
        }
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            docs = data.get("documents", [])
            for doc in docs:
                doc["search_area"] = area["name"]
                doc["search_category"] = name
            results.extend(docs)
            if data.get("meta", {}).get("is_end", True):
                break
            time.sleep(0.15)
        except Exception as e:
            print(f"    [카테고리 오류] {area['name']} / {name}: {e}")
            break

    return results


def search_by_keyword(area: dict, keyword: str) -> list:
    """키워드로 장소 검색 (최대 45개)"""
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    results = []

    for page in range(1, 4):
        params = {
            "query": keyword,
            "x": area["x"],
            "y": area["y"],
            "radius": 800,
            "page": page,
            "size": 15,
            "sort": "accuracy",
        }
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            docs = data.get("documents", [])
            for doc in docs:
                doc["search_area"] = area["name"]
                doc["search_category"] = keyword
            results.extend(docs)
            if data.get("meta", {}).get("is_end", True):
                break
            time.sleep(0.15)
        except Exception as e:
            print(f"    [키워드 오류] {area['name']} / {keyword}: {e}")
            break

    return results


def auto_fit_columns(ws):
    """엑셀 컬럼 너비 자동 조정"""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 60)


def main():
    all_places = []
    seen_ids: set[str] = set()

    print("=" * 50)
    print("  카카오 로컬 API - 신규 오픈 매장 수집")
    print("=" * 50)

    # 1단계: 카테고리 코드 검색
    print("\n[1단계] 카테고리 코드 검색")
    for area in AREAS:
        for code, name in CATEGORY_CODES.items():
            print(f"  {area['name']} / {name} ...", end=" ", flush=True)
            places = search_by_category(area, code, name)
            new = 0
            for p in places:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    all_places.append(p)
                    new += 1
            print(f"{len(places)}건 수집, 신규 {new}건")
            time.sleep(0.2)

    # 2단계: 키워드 검색
    print("\n[2단계] 키워드 검색")
    for area in AREAS:
        for keyword in SEARCH_KEYWORDS:
            print(f"  {area['name']} / {keyword} ...", end=" ", flush=True)
            places = search_by_keyword(area, keyword)
            new = 0
            for p in places:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    all_places.append(p)
                    new += 1
            print(f"{len(places)}건 수집, 신규 {new}건")
            time.sleep(0.2)

    print(f"\n총 {len(all_places)}개 매장 수집 완료")

    # 3단계: 데이터 정리
    rows = []
    for p in all_places:
        rows.append({
            "매장명":        p.get("place_name", ""),
            "카테고리":      p.get("category_name") or p.get("search_category", ""),
            "주소":          p.get("road_address_name") or p.get("address_name", ""),
            "전화번호":      p.get("phone", ""),
            "카카오맵 링크": p.get("place_url", ""),
            "검색지역":      p.get("search_area", ""),
        })

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["매장명", "주소"])

    output_cols = ["매장명", "카테고리", "주소", "전화번호", "카카오맵 링크", "검색지역"]

    # 4단계: 엑셀 저장
    output_path = "new_stores.xlsx"
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df[output_cols].to_excel(writer, index=False, sheet_name="신규매장")
        auto_fit_columns(writer.sheets["신규매장"])

    print(f"\n{'=' * 50}")
    print(f"  저장 완료: {output_path}")
    print(f"  총 매장 수: {len(df)}개")
    print("=" * 50)


if __name__ == "__main__":
    main()
