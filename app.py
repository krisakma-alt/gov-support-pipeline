"""
정부 지원사업 공고 뷰어 - 영리봇 맞춤
- 영리봇 정식 시작 이전 데이터(옛날 서비스 프로필) 자동 숨김
- 마감 지난 공고 자동 숨김
"""
import os
from datetime import datetime, date
from flask import Flask, render_template, request
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "10X1w5WmoY-1Blr4fEedEcfGUBl7AvKcIEFAJEBRpQQY"
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service-account.json")

# 영리봇 정식 데이터 시작 시점 (이전 데이터는 옛날 프로필로 분석됨)
YOUNGRIBOT_START_DATE = date(2026, 3, 25)

app = Flask(__name__)


def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:len(fmt)+5], fmt).date()
        except ValueError:
            continue
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
    """마감 지나지 않은 공고만 True"""
    deadline_str = item.get("신청 마감일", "").strip()
    if not deadline_str or deadline_str in ("미확인", "-", "상시"):
        return True
    deadline = parse_date(deadline_str)
    if deadline is None:
        return True
    return deadline >= date.today()


def is_recent(item):
    """영리봇 정식 데이터인지 확인"""
    collected = parse_date(item.get("수집일", "").strip())
    if collected is None:
        return False
    return collected >= YOUNGRIBOT_START_DATE


@app.route("/")
def index():
    sort = request.args.get("sort", "recent")
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
        items=items,
        sort=sort,
        query=query,
        show_old=show_old,
        show_expired=show_expired,
        total_raw=total_raw,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
