"""
영리봇 정부 지원사업 자동 모니터링 파이프라인
기업마당 + NIPA에서 공고를 수집 → 키워드 필터링 → Claude 적합도 판별 → Google Sheets 저장
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
import datetime

import requests
from bs4 import BeautifulSoup
import anthropic
from dotenv import load_dotenv

from config import (
    BUSINESS_PROFILE,
    INCLUDE_KEYWORDS,
    EXCLUDE_KEYWORDS,
)

load_dotenv(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    override=True,
)

# Windows 콘솔 UTF-8 출력 설정
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
SHEET_ID = "10X1w5WmoY-1Blr4fEedEcfGUBl7AvKcIEFAJEBRpQQY"

# Phase 4에서 변경될 새 컬럼 구조
SHEET_HEADERS = [
    "공고명", "출처 사이트", "신청 마감일", "개인사업자 가능",
    "지원 금액", "영리봇 적합도", "적합도 근거", "공고 URL",
    "수집일", "상태", "source_id",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 적합도 기준점 (이 점수 이상만 Sheets에 저장)
MIN_RELEVANCE_SCORE = 4


# ── 수집 함수 ──────────────────────────────────────────────

def fetch_bizinfo() -> list[dict]:
    """기업마당 HTML에서 공고를 수집합니다."""
    print("  [기업마당] 공고 수집 중...")
    announcements = []
    try:
        url = "https://www.bizinfo.go.kr/web/lay1/bbs/S1T122C128/AS/74/list.do"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tbody tr")
        for row in rows[:15]:
            tds = row.find_all("td")
            if len(tds) < 8:
                continue
            title_td = tds[2]
            a_tag = title_td.find("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            if title and len(title) > 5:
                announcements.append({
                    "source": "기업마당",
                    "title": title,
                    "link": f"https://www.bizinfo.go.kr{href}" if href.startswith("/") else href,
                    "description": "",
                    "date": tds[6].get_text(strip=True) if len(tds) > 6 else "",
                })
    except Exception as e:
        print(f"  [기업마당] HTML 크롤링 실패: {e}")

    print(f"  [기업마당] {len(announcements)}건 수집 완료")
    return announcements


def fetch_nipa() -> list[dict]:
    """NIPA(정보통신산업진흥원) 사업공고를 수집합니다."""
    print("  [NIPA] 공고 수집 중...")
    announcements = []
    try:
        url = "https://www.nipa.kr/home/2-2"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tbody tr")
        for row in rows[:15]:
            tds = row.find_all("td")
            if len(tds) < 5:
                continue
            a_tag = tds[2].find("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            date_str = tds[4].get_text(strip=True) if len(tds) > 4 else ""
            if title and len(title) > 5:
                announcements.append({
                    "source": "NIPA",
                    "title": title,
                    "link": f"https://www.nipa.kr{href}" if href.startswith("/") else href,
                    "description": "",
                    "date": date_str,
                })
    except Exception as e:
        print(f"  [NIPA] 수집 실패: {e}")

    print(f"  [NIPA] {len(announcements)}건 수집 완료")
    return announcements


# ── 유틸리티 ──────────────────────────────────────────────

def make_source_id(link: str, title: str = "", source: str = "") -> str:
    """공고 고유 ID를 생성합니다."""
    if link:
        m = re.search(r"pblancId=([\w]+)", link)
        if m:
            return m.group(1)
        m = re.search(r"bbsId=([\w]+)", link)
        if m:
            return m.group(1)
        return hashlib.sha1(link.encode()).hexdigest()[:12]
    return hashlib.sha1(f"{source}{title}".encode()).hexdigest()[:12]


def clean_text(text: str) -> str:
    """잘못된 유니코드(surrogate 등)를 제거합니다."""
    return text.encode("utf-8", errors="ignore").decode("utf-8")


# ── 필터링 ──────────────────────────────────────────────

REGIONAL_KEYWORDS = [
    "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
    "수원", "성남", "고양", "용인", "창원", "청주", "천안", "전주",
    "안산", "안양", "남양주", "화성", "평택", "의정부", "시흥", "파주",
    "김해", "포항", "진주", "구미", "경주", "여수", "순천", "목포",
    "춘천", "원주", "강릉", "충주", "아산", "익산", "군산",
]

_REGION_BRACKET_RE = re.compile(
    r"[\[\(](" + "|".join(REGIONAL_KEYWORDS) + r")[^\]\)]*[\]\)]"
)


def filter_regional(announcements: list[dict]) -> tuple[list[dict], int]:
    """지역 한정 공고를 제외합니다."""
    kept, removed = [], 0
    for ann in announcements:
        title = ann.get("title", "")
        if _REGION_BRACKET_RE.search(title):
            removed += 1
            continue
        kept.append(ann)
    return kept, removed


def filter_by_keywords(announcements: list[dict]) -> tuple[list[dict], int]:
    """config의 포함/제외 키워드로 1차 필터링합니다."""
    kept, removed = [], 0
    for ann in announcements:
        title = ann.get("title", "")

        # 제외 키워드 체크
        excluded = False
        for kw in EXCLUDE_KEYWORDS:
            if kw in title:
                excluded = True
                break
        if excluded:
            removed += 1
            continue

        # 포함 키워드 체크 (하나라도 매치하면 통과)
        matched = False
        for kw in INCLUDE_KEYWORDS:
            if kw.lower() in title.lower():
                matched = True
                break
        if not matched:
            removed += 1
            continue

        kept.append(ann)
    return kept, removed


# ── Claude 판별 ──────────────────────────────────────────

def evaluate_with_claude(announcements: list[dict]) -> list[dict]:
    """Claude API로 공고 목록을 일괄 판별합니다.
    각 공고에 대해 적합도 점수 + 구조화된 정보를 반환합니다."""
    if not announcements:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 공고 목록 텍스트 생성
    ann_lines = []
    for i, a in enumerate(announcements, 1):
        ann_lines.append(f"[{i}] [{a['source']}] {clean_text(a['title'])}")
        if a.get("date"):
            ann_lines.append(f"    등록일: {a['date']}")
        if a.get("link"):
            ann_lines.append(f"    링크: {a['link']}")
        ann_lines.append("")

    ann_text = "\n".join(ann_lines)

    prompt = f"""당신은 정부 지원사업 전문가입니다. 아래 사업자 정보를 기준으로 각 공고의 적합도를 판별하세요.

## 사업자 정보
{BUSINESS_PROFILE}

## 판별 대상 공고 목록
{ann_text}

## 판별 기준
각 공고에 대해 아래 7개 항목을 판별하세요:
1. 개인사업자 신청 가능 여부 (Y/N/불확실)
2. 소상공인 대상 여부 (Y/N/불확실)
3. AI/디지털 관련 여부 (Y/N)
4. 지원 금액 규모 (공고 제목에서 추정, 없으면 "미확인")
5. 신청 마감일 (공고 제목에서 추정, 없으면 "미확인")
6. 영리봇 적합도 점수 (1~10, 10이 가장 적합)
7. 적합도 판단 근거 (한 줄)

## 출력 형식
반드시 아래 JSON 배열 형식으로만 출력하세요. 다른 텍스트 없이 JSON만 출력:
```json
[
  {{
    "index": 1,
    "individual_ok": "Y",
    "small_biz_ok": "Y",
    "ai_related": "Y",
    "budget": "최대 1억원",
    "deadline": "2026-04-30",
    "score": 8,
    "reason": "소상공인 AI 도입 지원으로 영리봇과 직접 관련"
  }}
]
```"""

    print(f"\n  [Claude] {len(announcements)}건 판별 중...")

    # 재시도 로직 (API 과부하 대응, Sonnet → Haiku 폴백)
    MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    MAX_RETRIES = 3
    result_text = ""
    for model in MODELS:
        success = False
        for attempt in range(MAX_RETRIES):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )
                result_text = response.content[0].text.strip()
                success = True
                break
            except anthropic.APIStatusError as e:
                if e.status_code == 529 and attempt < MAX_RETRIES - 1:
                    wait = (attempt + 1) * 5
                    print(f"  [Claude] {model} 과부하, {wait}초 후 재시도...")
                    time.sleep(wait)
                elif e.status_code == 529 and model != MODELS[-1]:
                    print(f"  [Claude] {model} 불가, 다음 모델로 전환...")
                    break
                else:
                    raise
        if success:
            break

    # JSON 추출 (```json ... ``` 블록이 있으면 그 안에서 추출)
    json_match = re.search(r"```json\s*\n?(.*?)\n?```", result_text, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # JSON 배열 직접 추출
        json_match = re.search(r"\[.*\]", result_text, re.DOTALL)
        json_str = json_match.group(0) if json_match else result_text

    try:
        evaluations = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"  [Claude] JSON 파싱 실패: {e}")
        print(f"  [Claude] 원문: {result_text[:500]}")
        return []

    # 원본 공고 데이터와 판별 결과 합치기
    results = []
    for ev in evaluations:
        idx = ev.get("index", 0) - 1
        if 0 <= idx < len(announcements):
            ann = announcements[idx]
            results.append({
                "title": ann["title"],
                "source": ann["source"],
                "link": ann.get("link", ""),
                "individual_ok": ev.get("individual_ok", "불확실"),
                "small_biz_ok": ev.get("small_biz_ok", "불확실"),
                "ai_related": ev.get("ai_related", "N"),
                "budget": ev.get("budget", "미확인"),
                "deadline": ev.get("deadline", "미확인"),
                "score": ev.get("score", 0),
                "reason": ev.get("reason", ""),
                "source_id": make_source_id(
                    ann.get("link", ""), ann["title"], ann["source"]
                ),
            })

    # 점수 높은 순 정렬
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ── Google Sheets ──────────────────────────────────────────

def fetch_existing_source_ids(key_path: str) -> set[str]:
    """Google Sheets에 저장된 source_id 목록을 반환합니다."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        key_path,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    ws = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    all_values = ws.get_all_values()
    if not all_values:
        return set()

    header = all_values[0]
    existing_ids: set[str] = set()

    sid_col = header.index("source_id") if "source_id" in header else None
    link_col = header.index("공고 URL") if "공고 URL" in header else None
    # 기존 형식 호환
    if link_col is None:
        link_col = header.index("링크") if "링크" in header else None

    for row in all_values[1:]:
        if sid_col is not None and len(row) > sid_col and row[sid_col]:
            existing_ids.add(row[sid_col])
        elif link_col is not None and len(row) > link_col and row[link_col]:
            existing_ids.add(make_source_id(row[link_col]))

    return existing_ids


def save_to_sheets(results: list[dict], key_path: str, timestamp: str):
    """판별 결과를 Google Sheets에 저장합니다."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        key_path,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    ws = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1

    existing = ws.get_all_values()
    if not existing or existing[0] != SHEET_HEADERS:
        # 헤더가 다르면 새로 설정
        if not existing:
            ws.insert_row(SHEET_HEADERS, index=1)
        else:
            ws.update(values=[SHEET_HEADERS], range_name="A1:K1")

    new_rows = [
        [
            r["title"],
            r["source"],
            r["deadline"],
            r["individual_ok"],
            r["budget"],
            r["score"],
            r["reason"],
            r["link"],
            timestamp,
            "신규",
            r["source_id"],
        ]
        for r in results
    ]
    ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    print(f"  [Google Sheets] {len(new_rows)}건 저장 완료.")


# ── 메인 ──────────────────────────────────────────────

def setup_logging():
    """logs/ 폴더에 날짜별 로그 파일을 생성합니다."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"{today}.log")

    # 콘솔 + 파일 동시 출력
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def main():
    log_file = setup_logging()
    log = logging.getLogger(__name__)

    log.info("=" * 60)
    log.info("  영리봇 정부 지원사업 자동 모니터링")
    log.info("  (기업마당 + NIPA → 키워드 필터 → Claude 판별)")
    log.info("=" * 60)

    if not ANTHROPIC_API_KEY:
        log.error("오류: .env 파일에 ANTHROPIC_API_KEY가 없습니다.")
        sys.exit(1)

    # 1. 공고 수집
    print("\n[1단계] 공고 수집")
    print("-" * 40)
    bizinfo_list = fetch_bizinfo()
    nipa_list = fetch_nipa()
    all_announcements = bizinfo_list + nipa_list
    print(f"  → 총 {len(all_announcements)}건 수집")

    # 2. 지역 한정 공고 필터링
    all_announcements, region_removed = filter_regional(all_announcements)
    print(f"  → 지역 한정 {region_removed}건 제외")

    # 3. 키워드 필터링
    print("\n[2단계] 키워드 필터링")
    print("-" * 40)
    filtered, keyword_removed = filter_by_keywords(all_announcements)
    print(f"  → 키워드 매칭 {len(filtered)}건 (미매칭 {keyword_removed}건 제외)")

    if not filtered:
        print("\n키워드에 매칭되는 공고가 없습니다.")
        sys.exit(0)

    # 4. 중복 제거
    already_seen: set[str] = set()
    if GOOGLE_CREDENTIALS_PATH and os.path.exists(GOOGLE_CREDENTIALS_PATH):
        try:
            already_seen = fetch_existing_source_ids(GOOGLE_CREDENTIALS_PATH)
            print(f"  → 기존 저장 공고 {len(already_seen)}건 확인")
        except Exception as e:
            print(f"  → 중복 체크 실패 (전체 분석 진행): {e}")

    new_announcements = [
        ann for ann in filtered
        if make_source_id(ann.get("link", ""), ann.get("title", ""), ann.get("source", ""))
        not in already_seen
    ]
    skipped = len(filtered) - len(new_announcements)
    if skipped:
        print(f"  → 이미 분석된 {skipped}건 제외")

    if not new_announcements:
        print("\n모든 공고가 이미 분석되었습니다.")
        sys.exit(0)

    # 5. Claude 판별
    print(f"\n[3단계] Claude AI 판별 ({len(new_announcements)}건)")
    print("-" * 40)
    try:
        results = evaluate_with_claude(new_announcements)
    except Exception as e:
        print(f"  [Claude API 오류] {e}")
        sys.exit(1)

    # 결과 출력
    print(f"\n  판별 완료: {len(results)}건")
    for r in results:
        emoji = "★" if r["score"] >= MIN_RELEVANCE_SCORE else "☆"
        print(f"  {emoji} [{r['score']:2d}점] {r['title'][:50]}")
        print(f"       개인사업자={r['individual_ok']} | {r['reason']}")

    # 적합도 기준 이상만 필터
    qualified = [r for r in results if r["score"] >= MIN_RELEVANCE_SCORE]
    print(f"\n  → 적합 공고 {len(qualified)}건 (점수 {MIN_RELEVANCE_SCORE}점 이상)")

    if not qualified:
        print("\n적합한 공고가 없습니다.")
        sys.exit(0)

    # 6. Google Sheets 저장
    print(f"\n[4단계] Google Sheets 저장")
    print("-" * 40)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not GOOGLE_CREDENTIALS_PATH:
        print("  .env에 GOOGLE_CREDENTIALS_PATH가 없어 건너뜁니다.")
    elif not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        print(f"  파일을 찾을 수 없습니다: {GOOGLE_CREDENTIALS_PATH}")
    else:
        try:
            save_to_sheets(qualified, GOOGLE_CREDENTIALS_PATH, timestamp)
        except Exception as e:
            print(f"  저장 실패: {e}")

    print("\n" + "=" * 60)
    print("  완료!")
    print("=" * 60)


if __name__ == "__main__":
    main()
