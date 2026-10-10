from io import BytesIO
import json
import os
from pathlib import Path
import sys

from flask import Flask, jsonify, render_template, request, send_file
from gtts import gTTS
from gtts.tts import gTTSError

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

from supabase_store import SupabaseError, SupabaseStore

app = Flask(__name__)
DATA_FILE = Path(app.root_path) / "data.json"

# Có SUPABASE_URL + key thì lưu trên Supabase, không thì lưu vào data.json như cũ
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
store = SupabaseStore(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Accents for English come from different Google domains (tld)
ALLOWED_LANGS = {"en", "vi"}
ALLOWED_TLDS = {"com", "co.uk", "com.au", "co.in", "ca"}


def load_data():
    if not DATA_FILE.exists():
        return {"items": [], "books": []}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"items": [], "books": []}
    if not isinstance(data, dict):
        return {"items": [], "books": []}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    books = data.get("books") if isinstance(data.get("books"), list) else []
    return {"items": items, "books": books}


def save_data(data):
    payload = {
        "items": data.get("items", []),
        "books": data.get("books", []),
    }
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def clean_data(data):
    """Keep only well-formed notebooks and sentences; drop sentences whose notebook is gone."""
    def text(v):
        return v.strip() if isinstance(v, str) else ""

    books, book_ids = [], set()
    for b in data.get("books") or []:
        if not isinstance(b, dict) or not text(b.get("id")) or not text(b.get("name")):
            continue
        if b["id"] in book_ids:
            continue
        book_ids.add(b["id"])
        books.append(b)

    items, item_ids = [], set()
    for it in data.get("items") or []:
        if not isinstance(it, dict) or not text(it.get("id")) or it["id"] in item_ids:
            continue
        if not text(it.get("term")) or not text(it.get("meaning")) or it.get("notebookId") not in book_ids:
            continue
        item_ids.add(it["id"])
        items.append(it)
    return {"items": items, "books": books}


@app.route("/api/data", methods=["GET", "POST"])
def api_data():
    if request.method == "GET":
        if store is None:
            return jsonify(load_data())
        try:
            return jsonify(store.load())
        except SupabaseError as e:
            app.logger.error(e)
            return jsonify(error="Could not load data from Supabase."), 502

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error="Invalid JSON payload."), 400
    data = clean_data(data)

    if store is None:
        save_data(data)
        return jsonify(success=True)
    try:
        store.save(data)
    except SupabaseError as e:
        app.logger.error(e)
        return jsonify(error="Could not save data to Supabase."), 502
    return jsonify(success=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/speak", methods=["POST"])
def speak():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    lang = data.get("lang", "en")
    tld = data.get("tld", "com")
    slow = bool(data.get("slow", False))

    if not text:
        return jsonify(error="Type some text first."), 400
    if len(text) > 5000:
        return jsonify(error="Text is too long. Keep it under 5000 characters."), 400
    if lang not in ALLOWED_LANGS:
        lang = "en"
    if tld not in ALLOWED_TLDS:
        tld = "com"

    try:
        buffer = BytesIO()
        gTTS(text=text, lang=lang, tld=tld, slow=slow).write_to_fp(buffer)
        buffer.seek(0)
    except gTTSError as e:
        return jsonify(error=f"Google TTS failed: {e}. Check your internet connection."), 502

    return send_file(buffer, mimetype="audio/mpeg", download_name="speech.mp3")


def import_json_to_supabase():
    """python app.py import-json : copy everything in data.json up to Supabase."""
    if store is None:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env first.")
    data = clean_data(load_data())
    existing = store.load()
    # Gộp với dữ liệu đã có trên Supabase để không xoá mất gì
    books = {b["id"]: b for b in existing["books"]} | {b["id"]: b for b in data["books"]}
    items = {i["id"]: i for i in existing["items"]} | {i["id"]: i for i in data["items"]}
    store.save({"books": list(books.values()), "items": list(items.values())})
    print(f"Imported {len(data['books'])} notebooks and {len(data['items'])} sentences from data.json.")


if __name__ == "__main__":
    if sys.argv[1:] == ["import-json"]:
        import_json_to_supabase()
        sys.exit()
    print("Storage:", f"Supabase ({SUPABASE_URL})" if store else f"local file {DATA_FILE}")
    # Listen on all network interfaces so other devices (phone, laptop) can open it.
    # Debug mode stays off here: its interactive debugger must not be reachable from the network.
    # Turn it on locally with: FLASK_DEBUG=1 python app.py
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
