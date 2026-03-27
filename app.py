"""
정부 지원사업 공고 매칭 웹앱
실행: python app.py → http://localhost:5000 자동 오픈
"""

import datetime
import os
from threading import Timer
import webbrowser

from flask import Flask, jsonify, render_template, request

from main import (
    ANTHROPIC_API_KEY,
    GOOGLE_CREDENTIALS_PATH,
    analyze_with_claude,
    fetch_bizinfo,
    fetch_existing_source_ids,
    fetch_nipa,
    filter_regional,
    make_source_id,
    parse_top5_from_analysis,
    save_to_sheets,
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB


def extract_file_text(file) -> str:
    """업로드된 파일에서 텍스트를 추출합니다."""
    name = file.filename.lower()
    if name.endswith(".pdf"):
        import pypdf
        reader = pypdf.PdfReader(file)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return file.read().decode("utf-8", errors="ignore")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": ".env에 ANTHROPIC_API_KEY가 없습니다."}), 500

    # 입력 수집
    purpose_text = request.form.get("purpose", "").strip()
    file = request.files.get("file")
    file_text = ""
    if file and file.filename:
        try:
            file_text = extract_file_text(file)
        except Exception as e:
            return jsonify({"error": f"파일 읽기 실패: {e}"}), 400

    purpose = "\n\n".join(filter(None, [purpose_text, file_text]))
    if not purpose:
        return jsonify({"error": "사업 설명을 입력하거나 파일을 업로드해주세요."}), 400

    # 공고 수집 + 지역 필터링
    all_ann = fetch_bizinfo() + fetch_nipa()
    all_ann, filtered_count = filter_regional(all_ann)

    # 중복 제거 (이미 Sheets에 있는 공고 제외)
    already_seen: set = set()
    if GOOGLE_CREDENTIALS_PATH and os.path.exists(GOOGLE_CREDENTIALS_PATH):
        try:
            already_seen = fetch_existing_source_ids(GOOGLE_CREDENTIALS_PATH)
        except Exception as e:
            print(f"[중복 체크 실패] {e}")

    new_ann = [
        a for a in all_ann
        if make_source_id(a.get("link", ""), a.get("title", ""), a.get("source", ""))
        not in already_seen
    ]
    skipped = len(all_ann) - len(new_ann)

    if not new_ann:
        return jsonify({
            "results": [],
            "stats": {
                "total": len(all_ann),
                "filtered": filtered_count,
                "skipped": skipped,
                "new": 0,
            },
            "message": "현재 수집된 공고가 모두 이미 분석된 상태입니다. 새 공고가 올라오면 다시 시도해주세요.",
        })

    # Claude 분석
    analysis = analyze_with_claude(purpose, new_ann)
    top5 = parse_top5_from_analysis(analysis)

    # Google Sheets 저장
    sheets_saved = False
    if GOOGLE_CREDENTIALS_PATH and os.path.exists(GOOGLE_CREDENTIALS_PATH) and top5:
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_to_sheets(top5, GOOGLE_CREDENTIALS_PATH, timestamp)
            sheets_saved = True
        except Exception as e:
            print(f"[Sheets 저장 실패] {e}")

    return jsonify({
        "results": top5,
        "stats": {
            "total": len(all_ann),
            "filtered": filtered_count,
            "skipped": skipped,
            "new": len(new_ann),
        },
        "sheets_saved": sheets_saved,
    })


if __name__ == "__main__":
    Timer(1, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="localhost", port=5000, debug=False)
