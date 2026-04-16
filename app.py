"""
정부 지원사업 공고 뷰어 - 영리봇 맞춤 (필터링 + AI 분석)
"""
import os
import re
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify
import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
import anthropic

SHEET_ID = "10X1w5WmoY-1Blr4fEedEcfGUBl7AvKcIEFAJEBRpQQY"
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service-account.json")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
YOUNGRIBOT_START_DATE = date(2026, 3, 25)

app = Flask(__name__)
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
analysis_cache = {}


def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    m = re.search(r'(\d{4})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})', s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def fetch_announcements():
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_PATH,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    ws = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    items = []
    for row in rows[1:]:
        padded = row + [""] * (len(header) - len(row))
        items.append(dict(zip(header, padded)))
    return items


def get_score(item):
    raw = item.get("영리봇 적합도") or item.get("적합도 점수") or ""
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError):
        return 0


def is_active(item):
    deadline = parse_date(item.get("신청 마감일", ""))
    if deadline is None:
        # 날짜 없거나 못 읽으면 수집일 기준 30일 이내면 표시
        collected = parse_date(item.get("수집일", ""))
        if collected is None:
            return False
        return (date.today() - collected).days <= 30
    return deadline >= date.today()


def is_recent(item):
    collected = parse_date(item.get("수집일", "").strip())
    if collected is None:
        return False
    return collected >= YOUNGRIBOT_START_DATE


@app.route("/")
def index():
    sort = request.args.get("sort", "score")
    query = request.args.get("q", "").strip().lower()
    show_old = request.args.get("show_old") == "1"
    show_expired = request.args.get("show_expired") == "1"

    try:
        items = fetch_announcements()
    except Exception as e:
        return f"<h1>Sheets 읽기 실패</h1><pre>{e}</pre>", 500

    total_raw = len(items)

    if not show_old:
        items = [it for it in items if is_recent(it)]
    if not show_expired:
        items = [it for it in items if is_active(it)]

    if query:
        items = [
            it for it in items
            if query in (it.get("공고명", "") + " " + it.get("적합도 근거", "")).lower()
        ]

    if sort == "score":
        items.sort(key=get_score, reverse=True)
    elif sort == "recent":
        items.sort(key=lambda it: it.get("수집일", ""), reverse=True)
    elif sort == "deadline":
        items.sort(key=lambda it: it.get("신청 마감일", "9999"))

    return render_template(
        "index.html",
        items=items, sort=sort, query=query,
        show_old=show_old, show_expired=show_expired,
        total_raw=total_raw,
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    title = data.get("title") or ""

    if not url:
        return jsonify({"error": "URL이 없습니다"}), 400
    if not ai_client:
        return jsonify({"error": "ANTHROPIC_API_KEY가 설정되지 않았습니다"}), 500

    if url in analysis_cache:
        return jsonify({"analysis": analysis_cache[url]})

    # 공고 본문 스크래핑
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        content = soup.get_text(separator="\n", strip=True)[:6000]
    except Exception as e:
        return jsonify({"error": f"공고 페이지를 불러오지 못했습니다: {e}"}), 500

    prompt = f"""당신은 정부 지원사업 컨설턴트입니다. 영리봇 사업주 입장에서 아래 공고를 친절하고 쉽게 해석해주세요.

[영리봇 서비스 설명]
- QR로 매장 영수증을 촬영하면 AI(Claude)가 네이버 리뷰 문구를 자동 생성해주는 SaaS
- 1인 개인사업자가 운영, Cloudflare Workers + D1 + KV 기반
- 매장(소상공인) 대상 B2B SaaS, AI/디지털전환 카테고리

[공고 제목]
{title}

[공고 본문 일부]
{content}

다음 형식으로만 답변하세요. 어려운 용어는 풀어서 설명해주세요:

## 📌 한 줄 요약
이 공고가 뭔지 1문장으로

## 💰 받을 수 있는 것
- 자금: 얼마까지
- 그 외 혜택: 컨설팅, 공간, 인력 등

## ✅ 영리봇 적합성
- 잘 맞는 점
- 안 맞는 점

## 📋 신청 자격
주요 조건과 영리봇이 충족하는지 (✅/❌)

## 🚀 신청 추천도
⭐ 1~5개 + 한 줄 이유

## ⏰ 지금 해야 할 일
신청까지 필요한 액션 1~3개"""

    try:
        resp = ai_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = resp.content[0].text
        analysis_cache[url] = analysis
        return jsonify({"analysis": analysis})
    except Exception as e:
        return jsonify({"error": f"AI 분석 실패: {e}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
