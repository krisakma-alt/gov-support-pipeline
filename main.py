"""
정부 지원사업 공고 매칭 툴
소상공인24 + 기업마당에서 공고를 수집하고 Claude가 적합한 TOP 5를 추천합니다.
"""

import hashlib
import os
import re
import sys
import time
import datetime
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
import anthropic
from dotenv import load_dotenv

load_dotenv()

# Windows 콘솔 UTF-8 출력 설정
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
SHEET_ID = "10X1w5WmoY-1Blr4fEedEcfGUBl7AvKcIEFAJEBRpQQY"
SHEET_HEADERS = ["순위", "공고명", "출처", "적합 이유", "링크", "분석일시", "source_id"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


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


def fetch_semas() -> list[dict]:
    """소상공인시장진흥공단(소상공인24) 공고를 수집합니다."""
    print("  [소상공인24] 공고 수집 중...")
    announcements = []
    urls_to_try = [
        "https://www.sbiz.or.kr/sup/bbs/listBbsMsg.do?bbsClCd=104",
        "https://www.semas.or.kr/web/main/contents.kmdc?p_id=01030200",
        "https://www.sbiz.or.kr/sup/bbs/listBbsMsg.do?bbsClCd=102",
    ]
    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")

            # 다양한 selector 시도
            items = (
                soup.select("table.board_list tbody tr")
                or soup.select("ul.list_wrap li")
                or soup.select(".bbs_list tr")
                or soup.select("tbody tr")
            )
            for item in items[:15]:
                a_tag = item.find("a")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                href = a_tag.get("href", "")
                # 날짜 추출 시도
                date_el = item.find(class_=lambda c: c and ("date" in c.lower() or "day" in c.lower()))
                date_str = date_el.get_text(strip=True) if date_el else ""
                if title and len(title) > 5:
                    announcements.append({
                        "source": "소상공인24",
                        "title": title,
                        "link": f"https://www.sbiz.or.kr{href}" if href.startswith("/") else href,
                        "description": "",
                        "date": date_str,
                    })
            if announcements:
                break
        except Exception as e:
            print(f"  [소상공인24] {url} 수집 실패: {e}")
        time.sleep(0.5)

    print(f"  [소상공인24] {len(announcements)}건 수집 완료")
    return announcements


def make_source_id(link: str, title: str = "", source: str = "") -> str:
    """공고 고유 ID를 생성합니다. URL의 pblancId 우선, 없으면 sha1 해시."""
    if link:
        m = re.search(r"pblancId=([\w]+)", link)
        if m:
            return m.group(1)
        m = re.search(r"bbsId=([\w]+)", link)
        if m:
            return m.group(1)
        return hashlib.sha1(link.encode()).hexdigest()[:12]
    return hashlib.sha1(f"{source}{title}".encode()).hexdigest()[:12]


def fetch_existing_source_ids(key_path: str) -> set[str]:
    """Google Sheets에 저장된 source_id 목록을 반환합니다.
    source_id 컬럼이 없는 기존 행은 링크 컬럼으로 소급 계산합니다."""
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
    link_col = header.index("링크") if "링크" in header else None

    for row in all_values[1:]:
        # source_id 컬럼이 있고 값이 있으면 그대로 사용
        if sid_col is not None and len(row) > sid_col and row[sid_col]:
            existing_ids.add(row[sid_col])
        # 없으면 링크 컬럼으로 소급 계산 (기존 데이터 호환)
        elif link_col is not None and len(row) > link_col and row[link_col]:
            existing_ids.add(make_source_id(row[link_col]))

    return existing_ids


REGIONAL_KEYWORDS = [
    # 광역시·도 (서울 제외 — 서울 한정 공고는 포함)
    "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
    # 시·군·구 (자주 등장하는 것만)
    "수원", "성남", "고양", "용인", "창원", "청주", "천안", "전주",
    "안산", "안양", "남양주", "화성", "평택", "의정부", "시흥", "파주",
    "김해", "포항", "진주", "구미", "경주", "여수", "순천", "목포",
    "춘천", "원주", "강릉", "충주", "아산", "익산", "군산",
]

# 제목 앞 괄호 패턴: [제주], [부산광역시] 등
_REGION_BRACKET_RE = re.compile(
    r"[\[\(](" + "|".join(REGIONAL_KEYWORDS) + r")[^\]\)]*[\]\)]"
)


def filter_regional(announcements: list[dict]) -> tuple[list[dict], int]:
    """지역 한정 공고를 제외하고 전국 대상 공고만 반환합니다."""
    kept, removed = [], 0
    for ann in announcements:
        title = ann.get("title", "")
        desc = ann.get("description", "")
        # 괄호 안에 지역명이 있으면 지역 한정으로 판단
        if _REGION_BRACKET_RE.search(title) or _REGION_BRACKET_RE.search(desc):
            removed += 1
            continue
        kept.append(ann)
    return kept, removed


def clean_text(text: str) -> str:
    """잘못된 유니코드(surrogate 등)를 제거합니다."""
    return text.encode("utf-8", errors="ignore").decode("utf-8")


def format_announcements_for_claude(announcements: list[dict]) -> str:
    """공고 목록을 Claude 프롬프트용 텍스트로 변환합니다."""
    lines = []
    for i, a in enumerate(announcements, 1):
        lines.append(f"[{i}] [{clean_text(a['source'])}] {clean_text(a['title'])}")
        if a.get("description"):
            lines.append(f"    내용: {clean_text(a['description'][:200])}")
        if a.get("date"):
            lines.append(f"    날짜: {clean_text(a['date'])}")
        if a.get("link"):
            lines.append(f"    링크: {clean_text(a['link'])}")
        lines.append("")
    return "\n".join(lines)


def analyze_with_claude(purpose: str, announcements: list[dict]) -> str:
    """Claude API로 목적에 맞는 TOP 5 공고를 분석합니다."""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        purpose = clean_text(purpose)
        ann_text = format_announcements_for_claude(announcements)

        prompt = f"""당신은 정부 지원사업 전문가입니다.

## 사용자 사업 목적
{purpose}

## 수집된 공고 목록
{ann_text}

위 공고 목록에서 사용자의 사업 목적에 가장 적합한 공고 TOP 5를 선정하고,
각 공고에 대해 다음 형식으로 출력하세요:

---
### TOP [순위]: [공고 번호] [공고 제목]
- **출처**: [기관명]
- **적합 이유**: 이 공고가 사용자 사업과 맞는 구체적인 이유 (2~3문장)
- **신청 전략**: 이 공고에 지원할 때 강조할 포인트
- **링크**: [URL]
---

만약 공고 수가 부족하거나 적합한 공고가 없다면, 솔직하게 말하고
대신 찾아볼 다른 사이트나 키워드를 추천해주세요."""

        print("\n  [Claude] 분석 중...")

        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            result_text = ""
            for event in stream:
                if (
                    hasattr(event, "type")
                    and event.type == "content_block_delta"
                    and hasattr(event.delta, "type")
                    and event.delta.type == "text_delta"
                ):
                    chunk = event.delta.text
                    print(chunk, end="", flush=True)
                    result_text += chunk

        print()  # 줄바꿈
        return result_text
    except Exception as e:
        print(f"[Claude API 오류] {e}")
        return "Claude API 호출 실패: " + str(e)


def parse_top5_from_analysis(analysis: str) -> list[dict]:
    """Claude 분석 텍스트에서 TOP 5 구조화 데이터를 추출합니다."""
    if "Claude API 호출 실패" in analysis:
        return []
    results = []
    # "### TOP N:" 기준으로 블록 분리
    blocks = re.split(r"###\s+TOP\s+(\d+)\s*:", analysis)
    # blocks = [앞텍스트, "1", "1번내용", "2", "2번내용", ...]
    i = 1
    while i < len(blocks) - 1:
        rank = blocks[i].strip()
        content = blocks[i + 1]

        # 공고명: 블록 첫 줄에서 [번호] 제거 후 추출
        title_match = re.match(r"\s*(?:\[\d+\]\s*)?(.+?)(?:\n|$)", content)
        title = title_match.group(1).strip() if title_match else ""

        # 출처
        source_match = re.search(r"\*\*출처\*\*\s*[:\s]+(.+?)(?:\n|$)", content)
        source = source_match.group(1).strip() if source_match else ""

        # 적합 이유 (다음 **항목** 전까지)
        reason_match = re.search(
            r"\*\*적합\s*이유\*\*\s*[:\s]+(.+?)(?=\n-\s*\*\*|\Z)",
            content,
            re.DOTALL,
        )
        reason = reason_match.group(1).strip() if reason_match else ""

        # 링크: [텍스트](URL) 또는 직접 URL
        link_match = re.search(
            r"\*\*링크\*\*\s*[:\s]+(?:\[.*?\]\((https?://[^\)]+)\)|(https?://\S+))",
            content,
        )
        if link_match:
            link = (link_match.group(1) or link_match.group(2) or "").strip()
        else:
            link = ""

        results.append({
            "rank": rank,
            "title": title,
            "source": source,
            "reason": reason,
            "link": link,
        })
        i += 2

    return results


def save_to_sheets(rows: list[dict], key_path: str, timestamp: str):
    """파싱된 TOP 5를 Google Sheets에 저장합니다."""
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
    if not existing:
        ws.insert_row(SHEET_HEADERS, index=1)
    elif existing[0] != SHEET_HEADERS:
        # source_id 컬럼만 누락된 기존 시트 → 헤더에 추가
        old_headers_without_sid = [h for h in SHEET_HEADERS if h != "source_id"]
        if existing[0] == old_headers_without_sid:
            ws.update_cell(1, len(SHEET_HEADERS), "source_id")
        else:
            ws.insert_row(SHEET_HEADERS, index=1)

    new_rows = [
        [
            r["rank"],
            r["title"],
            r["source"],
            r["reason"],
            r["link"],
            timestamp,
            make_source_id(r["link"], r["title"], r["source"]),
        ]
        for r in rows
    ]
    ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    print(f"  [Google Sheets] {len(new_rows)}건 저장 완료.")


def save_results(purpose: str, announcements: list[dict], analysis: str):
    """결과를 results.txt에 저장합니다."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output_path = os.path.join(os.path.dirname(__file__), "results.txt")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write(f"정부 지원사업 매칭 결과\n")
        f.write(f"생성 시각: {timestamp}\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"[사업 목적]\n{purpose}\n\n")
        f.write(f"[수집된 공고 수] 총 {len(announcements)}건\n")

        # 수집 공고 목록
        f.write("\n[수집 공고 원문]\n")
        f.write("-" * 40 + "\n")
        f.write(format_announcements_for_claude(announcements))

        f.write("\n" + "=" * 60 + "\n")
        f.write("[AI 매칭 분석 결과]\n")
        f.write("=" * 60 + "\n\n")
        f.write(analysis)

    print(f"\n결과가 '{output_path}'에 저장되었습니다.")


def main():
    print("=" * 60)
    print("  정부 지원사업 공고 매칭 툴 (소상공인24 + 기업마당)")
    print("=" * 60)

    if not ANTHROPIC_API_KEY:
        print("오류: .env 파일에 ANTHROPIC_API_KEY가 없습니다.")
        print("  .env 파일을 생성하고 ANTHROPIC_API_KEY=your_key_here 를 추가하세요.")
        sys.exit(1)

    print("\n사업 목적을 입력하세요 (입력 후 빈 줄로 완료):")
    print("(예: 소상공인 디지털전환 실증사업 공급기업으로 등록하고 싶음)\n")

    lines = []
    while True:
        try:
            line = input()
            if line == "":
                if lines:
                    break
            else:
                lines.append(line)
        except EOFError:
            break

    purpose = "\n".join(lines).strip()
    if not purpose:
        print("사업 목적을 입력해주세요.")
        sys.exit(1)

    print(f"\n입력된 목적:\n{purpose}\n")
    print("공고 수집을 시작합니다...")
    print("-" * 40)

    # 공고 수집
    bizinfo_list = fetch_bizinfo()
    semas_list = fetch_semas()
    all_announcements = bizinfo_list + semas_list

    # 지역 한정 공고 필터링
    all_announcements, filtered_count = filter_regional(all_announcements)

    print(f"\n총 {len(all_announcements)}건의 공고를 수집했습니다. (지역 한정 {filtered_count}건 제외)")

    if not all_announcements:
        print("\n수집된 공고가 없습니다. 네트워크 연결을 확인하거나 나중에 다시 시도하세요.")
        sys.exit(1)

    # 이미 분석된 공고 중복 제거
    already_seen: set[str] = set()
    if GOOGLE_CREDENTIALS_PATH and os.path.exists(GOOGLE_CREDENTIALS_PATH):
        try:
            print("  [중복 체크] Google Sheets에서 기존 source_id 조회 중...")
            already_seen = fetch_existing_source_ids(GOOGLE_CREDENTIALS_PATH)
            print(f"  [중복 체크] 기존 저장 공고 {len(already_seen)}건 확인")
        except Exception as e:
            print(f"  [중복 체크] 조회 실패 (전체 분석으로 진행): {e}")

    new_announcements = [
        ann for ann in all_announcements
        if make_source_id(ann.get("link", ""), ann.get("title", ""), ann.get("source", ""))
        not in already_seen
    ]
    skipped = len(all_announcements) - len(new_announcements)

    if skipped:
        print(f"  [중복 제거] 이미 분석된 {skipped}건 제외 → 신규 {len(new_announcements)}건만 분석합니다.")
    if not new_announcements:
        print("\n모든 공고가 이미 분석되었습니다. 새로운 공고가 올라오면 다시 실행하세요.")
        sys.exit(0)

    print("\n" + "=" * 60)
    print("  Claude 분석 결과 (실시간 출력)")
    print("=" * 60)

    # Claude 분석 (신규 공고만)
    analysis = analyze_with_claude(purpose, new_announcements)

    # 파일 저장
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_results(purpose, new_announcements, analysis)

    # Google Sheets 저장
    if not GOOGLE_CREDENTIALS_PATH:
        print("\n  [Google Sheets] .env에 GOOGLE_CREDENTIALS_PATH가 없어 건너뜁니다.")
    elif not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        print(f"\n  [Google Sheets] 파일을 찾을 수 없습니다: {GOOGLE_CREDENTIALS_PATH}")
    else:
        try:
            top5 = parse_top5_from_analysis(analysis)
            if not top5:
                print("\n  [Google Sheets] 분석 결과에서 TOP 5를 파싱하지 못했습니다.")
            else:
                save_to_sheets(top5, GOOGLE_CREDENTIALS_PATH, timestamp)
        except Exception as e:
            print(f"\n  [Google Sheets] 저장 실패: {e}")


if __name__ == "__main__":
    main()
