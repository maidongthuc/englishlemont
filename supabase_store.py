"""Save notebooks and sentences in Supabase (tables: notebooks, sentences).

Talks to Supabase's REST API (PostgREST) with the service role key, so the key
stays on the server and is never sent to the browser.
"""
import requests

PAGE_SIZE = 1000  # Supabase returns at most 1000 rows per request by default
DELETE_BATCH = 100  # ids per DELETE request, keeps the URL short
TIMEOUT = 15


class SupabaseError(Exception):
    pass


class SupabaseStore:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update({"apikey": key, "Content-Type": "application/json"})
        # Key kiểu cũ (JWT, bắt đầu bằng "eyJ") cần thêm Authorization.
        # Key kiểu mới (sb_secret_...) không phải JWT: chỉ gửi qua header apikey.
        if key.startswith("eyJ"):
            self.session.headers["Authorization"] = f"Bearer {key}"

    def _request(self, method, table, **kwargs):
        try:
            r = self.session.request(method, f"{self.base}/{table}", timeout=TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise SupabaseError(f"Cannot reach Supabase: {e}") from e
        if r.status_code >= 400:
            raise SupabaseError(f"Supabase {method} {table} failed ({r.status_code}): {r.text[:300]}")
        return r

    def _select_all(self, table, order, select="*"):
        rows, start = [], 0
        while True:
            r = self._request(
                "GET", table,
                params={"select": select, "order": order},
                headers={"Range": f"{start}-{start + PAGE_SIZE - 1}"},
            )
            page = r.json()
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            start += PAGE_SIZE

    def _upsert(self, table, rows):
        for i in range(0, len(rows), PAGE_SIZE):
            self._request(
                "POST", table,
                json=rows[i:i + PAGE_SIZE],
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )

    def _delete_except(self, table, keep_ids):
        # Xoá các dòng không còn trong dữ liệu gửi lên.
        # Chỉ gửi id cần xoá, chia nhỏ từng nhóm để URL không quá dài.
        keep = set(keep_ids)
        current = [r["id"] for r in self._select_all(table, "id.asc", select="id")]
        to_delete = [i for i in current if i not in keep]
        for i in range(0, len(to_delete), DELETE_BATCH):
            quoted = ",".join('"' + x.replace('"', "") + '"' for x in to_delete[i:i + DELETE_BATCH])
            self._request("DELETE", table, params={"id": f"in.({quoted})"}, headers={"Prefer": "return=minimal"})

    # ---------- Dữ liệu theo định dạng của trang web ----------

    def load(self):
        books = [
            {"id": r["id"], "kind": r["kind"], "name": r["name"], "color": r["color"], "createdAt": r["created_at"]}
            for r in self._select_all("notebooks", "created_at.asc")
        ]
        items = [
            {"id": r["id"], "term": r["term"], "meaning": r["meaning"],
             "notebookId": r["notebook_id"], "createdAt": r["created_at"]}
            for r in self._select_all("sentences", "created_at.asc")
        ]
        return {"items": items, "books": books}

    def save(self, data):
        books = [
            {"id": b["id"], "kind": b.get("kind") or "notebook", "name": b["name"],
             "color": b.get("color") or "#ffd84d", "created_at": int(b.get("createdAt") or 0)}
            for b in data["books"]
        ]
        sentences = [
            {"id": it["id"], "notebook_id": it["notebookId"], "term": it["term"],
             "meaning": it["meaning"], "created_at": int(it.get("createdAt") or 0)}
            for it in data["items"]
        ]
        # Thứ tự: thêm/sửa sổ -> thêm/sửa câu -> xoá câu thừa -> xoá sổ thừa
        self._upsert("notebooks", books)
        self._upsert("sentences", sentences)
        self._delete_except("sentences", [s["id"] for s in sentences])
        self._delete_except("notebooks", [b["id"] for b in books])


# ---------- Kiểm tra kết nối: python supabase_store.py ----------

def check_connection(url, key):
    """Return (ok, message) after trying to read both tables."""
    if not url or not key:
        return False, "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing in .env."
    if key.startswith("sb_publishable_"):
        return False, "This is the publishable (public) key. Use the secret / service_role key."
    store = SupabaseStore(url, key)
    counts = {}
    for table in ("notebooks", "sentences"):
        try:
            r = store._request("GET", table, params={"select": "id"},
                               headers={"Range": "0-0", "Prefer": "count=exact"})
        except SupabaseError as e:
            text = str(e)
            if "Cannot reach" in text:
                return False, f"Cannot reach {url}. Check SUPABASE_URL and your internet connection.\n  {text}"
            if "(401)" in text or "(403)" in text:
                return False, f"Supabase rejected the key. Check SUPABASE_SERVICE_ROLE_KEY.\n  {text}"
            if "(404)" in text or "PGRST205" in text or "42P01" in text:
                return False, f"Table '{table}' not found. Run supabase_schema.sql in the SQL Editor.\n  {text}"
            return False, text
        counts[table] = r.headers.get("Content-Range", "*/?").split("/")[-1]
    return True, f"Connected to {url}: {counts['notebooks']} notebooks, {counts['sentences']} sentences."


if __name__ == "__main__":
    import os
    import sys
    from pathlib import Path

    env_file = Path(__file__).with_name(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    else:
        print("No .env file found next to supabase_store.py.")

    ok, message = check_connection(os.environ.get("SUPABASE_URL", "").strip(),
                                   os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip())
    print(("OK    " if ok else "ERROR ") + message)
    sys.exit(0 if ok else 1)
