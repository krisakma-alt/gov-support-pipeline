"""
정부 지원사업 공고 뷰어 - Google Sheets 데이터 표시
"""
import os
from flask import Flask, render_template, request
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "10X1w5WmoY-1Blr4fEedEcfGUBl7AvKcIEFAJEBRpQQY"
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "service-account.json")

app = Flask(__name__)


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
        return float(raw)
    except (ValueError, TypeError):
        return 0


@app.route("/")
def index():
    sort = request.args.get("sort", "score")
    query = request.args.get("q", "").strip().lower()

    try:
        items = fetch_announcements()
    except Exception as e:
        return f"<h1>Sheets 읽기 실패</h1><pre>{e}</pre>", 500

    if query:
        items = [
            it for it in items
            if query in (it.get("공고명", "") + " " + it.get("적합도 근거", "")).lower()
        ]

    if sort == "score":
        items.sort(key=get_score, reverse=True)
    elif sort == "recent":
        items.sort(key=lambda it: it.get("수집 시각", ""), reverse=True)
    elif sort == "deadline":
        items.sort(key=lambda it: it.get("신청 마감일", "9999"))

    return render_template("index.html", items=items, sort=sort, query=query)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
