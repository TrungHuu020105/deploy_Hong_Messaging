"""Pancake -> MySQL -> reviewed TikTok message queue. No network on import."""
from __future__ import annotations

import ast
import csv
import json
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from hong_database import (MySQLConnection, begin_write, concat_sql, insert_if_absent,
                           json_present_sql, load_environment, upsert)
from backend.domain import evaluate_price, price_rules_hash

ROOT = Path(__file__).resolve().parent
VN = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def today_vn():
    return datetime.now(VN).date()


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_settings(path=ROOT / ".env"):
    """Environment overrides local .env; do not log returned API key."""
    settings = load_environment(path)
    for key in ("PANCAKE_SHOP_ID", "PANCAKE_API_KEY"):
        settings[key] = os.environ.get(key, settings.get(key, ""))
        if not settings[key] or settings[key] == "replace_with_your_api_key":
            raise ValueError(f"Thiếu {key}. Điền .env hoặc biến môi trường.")
    if not settings["PANCAKE_SHOP_ID"].isdigit():
        raise ValueError("PANCAKE_SHOP_ID phải là số.")
    return settings


def date_bounds(start, end):
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if start > end:
        raise ValueError("Ngày bắt đầu phải trước hoặc trùng với ngày kết thúc.")
    begin = datetime.combine(start, datetime.min.time(), VN)
    finish = datetime.combine(end + timedelta(days=1), datetime.min.time(), VN)
    return int(begin.timestamp()), int(finish.timestamp())


def fetch_orders(settings, start, end, *, page_size=10, time_filter=3,
                 page_parameter="page", session=None, max_pages=10000):
    """Keep legacy API query defaults; callers can adjust to their POS contract.

    No silent truncation: repeated pages / malformed responses raise and the
    caller must not persist this partial result. Errors never include key URL.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    if page_size < 1 or max_pages < 1 or page_parameter not in {"page", "page_number"}:
        raise ValueError("Cấu hình phân trang không hợp lệ.")
    start_ts, end_ts = date_bounds(start, end)
    owned = session is None
    if owned:
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=["GET"], respect_retry_after_header=True)
        session.mount("https://", HTTPAdapter(max_retries=retries))
    url = f"https://pos.pages.fm/api/v1/shops/{settings['PANCAKE_SHOP_ID']}/orders"
    all_orders, seen = [], set()
    try:
        for page in range(1, max_pages + 1):
            params = {"api_key": settings["PANCAKE_API_KEY"], "startDateTime": start_ts,
                      "endDateTime": end_ts, "updateStatus": time_filter,
                      page_parameter: page, "page_size": page_size}
            try:
                response = session.get(url, params=params, timeout=(10, 60))
                response.raise_for_status()
                result = response.json()
            except (requests.RequestException, ValueError):
                raise RuntimeError(f"Không đọc được API ở trang {page}; chưa lưu đợt đồng bộ.") from None
            if not isinstance(result, dict) or result.get("success") is False:
                raise ValueError(f"API báo lỗi hoặc sai cấu trúc ở trang {page}.")
            rows = result.get("data")
            if not isinstance(rows, list):
                raise ValueError(f"Trường data trong phản hồi API phải là danh sách ở trang {page}.")
            for order in rows:
                if not isinstance(order, dict) or order.get("id") is None:
                    raise ValueError("Phản hồi API có đơn thiếu mã id; đợt đồng bộ chưa được lưu.")
                oid = str(order["id"])
                if oid in seen:
                    raise ValueError("API trả về mã đơn trùng giữa các trang. Hãy kiểm tra tham số phân trang page_parameter.")
                seen.add(oid)
            all_orders.extend(rows)
            if len(rows) < page_size:
                return all_orders
        raise RuntimeError("Đã vượt số trang tối đa (max_pages). Dữ liệu chưa được lưu vì có thể chưa đầy đủ.")
    finally:
        if owned:
            session.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_deleted_orders (
 shop_id TEXT NOT NULL,order_id TEXT NOT NULL,deleted_at TEXT NOT NULL,
 PRIMARY KEY(shop_id,order_id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
 id INTEGER PRIMARY KEY, shop_id TEXT NOT NULL, started_at TEXT NOT NULL,
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, row_count INTEGER NOT NULL,
 origin TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
 customer_key TEXT PRIMARY KEY, shop_id TEXT NOT NULL, display_name TEXT,
 phone TEXT, username_candidate TEXT, username TEXT COLLATE NOCASE,
 verified INTEGER NOT NULL DEFAULT 0, opted_out INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
 shop_id TEXT NOT NULL, order_id TEXT NOT NULL, customer_key TEXT NOT NULL,
 status INTEGER, source TEXT, amount REAL, amount_source TEXT,
 created_at TEXT, updated_at TEXT, status_at TEXT, update_date TEXT,
 date_run TEXT, raw_json TEXT NOT NULL, synced_at TEXT NOT NULL,
 PRIMARY KEY(shop_id, order_id),
 FOREIGN KEY(customer_key) REFERENCES customers(customer_key)
);
CREATE TABLE IF NOT EXISTS order_items (
 shop_id TEXT NOT NULL, order_id TEXT NOT NULL, line_no INTEGER NOT NULL,
 sku TEXT, product_name TEXT, quantity REAL, unit_price REAL,
 total_discount REAL, raw_json TEXT NOT NULL,
 PRIMARY KEY(shop_id, order_id, line_no),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS app_order_prices (
 shop_id TEXT NOT NULL,order_id TEXT NOT NULL,price_eligible INTEGER NOT NULL,
 rules_hash TEXT NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(shop_id,order_id),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_order_prices_eligible ON app_order_prices(price_eligible,rules_hash);
CREATE TABLE IF NOT EXISTS sync_run_orders (
 sync_run_id INTEGER NOT NULL, shop_id TEXT NOT NULL, order_id TEXT NOT NULL,
 position INTEGER NOT NULL,
 PRIMARY KEY(sync_run_id,shop_id,order_id),
 FOREIGN KEY(sync_run_id) REFERENCES sync_runs(id),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id)
);
CREATE TABLE IF NOT EXISTS status_events (
 shop_id TEXT NOT NULL, order_id TEXT NOT NULL, line_no INTEGER NOT NULL,
 status INTEGER, updated_at TEXT, raw_json TEXT NOT NULL,
 PRIMARY KEY(shop_id,order_id,line_no),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, campaign TEXT NOT NULL, shop_id TEXT NOT NULL,
 order_id TEXT NOT NULL, customer_key TEXT NOT NULL,
 username TEXT NOT NULL COLLATE NOCASE, body TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','sending','submitted','sent','failed','uncertain','cancelled')),
 created_at TEXT NOT NULL, attempted_at TEXT, finished_at TEXT, error TEXT,
 UNIQUE(campaign,shop_id,order_id,username),
 FOREIGN KEY(shop_id,order_id) REFERENCES orders(shop_id,order_id),
 FOREIGN KEY(customer_key) REFERENCES customers(customer_key)
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_key);
CREATE INDEX IF NOT EXISTS idx_messages_username ON messages(username,state,attempted_at);
CREATE TABLE IF NOT EXISTS message_events (
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, at TEXT NOT NULL,
 state TEXT NOT NULL, note TEXT,
 FOREIGN KEY(message_id) REFERENCES messages(id)
);
CREATE VIEW IF NOT EXISTS customer_stats AS
 SELECT c.*, COUNT(o.order_id) AS order_count,
 SUM(CASE WHEN o.status=3 THEN 1 ELSE 0 END) AS status3_orders,
 COALESCE(SUM(o.amount),0) AS observed_order_value,
 MAX(o.update_date) AS last_order_date
 FROM customers c LEFT JOIN orders o ON c.customer_key=o.customer_key
 GROUP BY c.customer_key;
"""


def connect_db(path=None, *, settings=None, initialize=True):
    """Default to MySQL; an explicit path opens SQLite for tests/legacy tools."""
    if path is None:
        return MySQLConnection(settings,initialize=initialize)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(messages)")}
    with db:
        if "require_verified" not in columns:
            # Preserve the stricter policy of already prepared messages.
            db.execute("ALTER TABLE messages ADD COLUMN require_verified INTEGER NOT NULL DEFAULT 1")
        if "sync_run_id" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN sync_run_id INTEGER REFERENCES sync_runs(id)")
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            # Old CSV migrations have a recoverable full batch. Never guess old
            # API batch membership; the next API sync will record it explicitly.
            for run in db.execute("SELECT * FROM sync_runs WHERE origin='legacy_csv' AND row_count>0 ORDER BY id"):
                rows = list(db.execute("""SELECT shop_id,order_id FROM orders WHERE shop_id=?
                    AND json_extract(raw_json,'$._legacy_row') IS NOT NULL ORDER BY order_id""", (run["shop_id"],)))
                if len(rows) == run["row_count"]:
                    db.executemany("INSERT OR IGNORE INTO sync_run_orders VALUES (?,?,?,?)",
                                   [(run["id"],r["shop_id"],r["order_id"],i) for i,r in enumerate(rows)])
                    break
            db.execute("PRAGMA user_version=2")
    return db


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def as_status(value):
    result = number(value)
    return int(result) if result is not None and result.is_integer() else None


def api_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Same UTC assumption as the original notebook. Raw value is retained.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


def username_candidate(value):
    value = str(value or "").strip().removeprefix("@").lower()
    return value if re.fullmatch(r"[a-z0-9_.]{2,24}", value) and not value.endswith(".") else None


def phone_identity(value):
    """Masked/invalid phone numbers must not merge unrelated customers."""
    value = str(value or "").strip()
    if not re.fullmatch(r"\+?[0-9\s().-]+",value):
        return None
    digits = re.sub(r"\D","",value)
    if not 8 <= len(digits) <= 15:
        return None
    if digits.startswith("84") and len(digits) == 11:
        digits = "0"+digits[2:]
    return digits


def normalize_order(order, shop_id):
    if order.get("id") is None:
        raise ValueError("Đơn hàng thiếu mã id.")
    shop_id, oid = str(shop_id), str(order["id"])
    customer = order.get("customer") or {}
    if not isinstance(customer, dict):
        customer = {}
    name = str(order.get("bill_full_name") or "").strip()
    phone = str(order.get("bill_phone_number") or "").strip()
    identity_phone = phone_identity(phone)
    candidate = username_candidate(name)
    cid = order.get("customer_id") or customer.get("id")
    # Do not merge unrelated buyers merely because they share a display name.
    identity = f"id:{cid}" if cid else (f"phone:{identity_phone}" if identity_phone else f"order:{oid}")
    key = f"{shop_id}:{identity}"
    raw_history = order.get("status_history") or []
    history = [h for h in raw_history if isinstance(h, dict)]
    # Business rule from the original notebook: use the LAST array entry,
    # without sorting it or preferring the top-level status/updated_at.
    last = raw_history[-1] if raw_history and isinstance(raw_history[-1],dict) else {}
    legacy = isinstance(order.get("_legacy_row"),dict)
    status = as_status(order.get("status")) if legacy else as_status(last.get("status"))
    status_at = api_datetime(last.get("updated_at"))
    updated = api_datetime(order.get("updated_at"))
    local_date = (datetime.fromisoformat(status_at).astimezone(VN).date().isoformat()
                  if status_at else (order.get("_legacy_update_date") if legacy else None))
    amount = number(order.get("buyer_total_amount"))
    amount_source = "buyer_total_amount" if amount is not None else None
    if amount == 0 and not legacy:
        # Exactly the old formula: discount of FIRST item, not sum of items.
        items = order.get("items") or []
        first = items[0] if items and isinstance(items[0],dict) else {}
        total, discount = number(order.get("total_price")), number(first.get("total_discount"))
        amount = total-discount if total is not None and discount is not None else None
        amount_source = "total_price_minus_first_item_discount" if amount is not None else None
    return {"shop_id": shop_id, "order_id": oid, "customer_key": key,
            "display_name": name, "phone": phone, "username_candidate": candidate,
            "status": status, "source": order.get("order_sources_name"),
            "amount": amount, "amount_source": amount_source,
            "created_at": api_datetime(order.get("inserted_at")), "updated_at": updated,
            "status_at": status_at, "update_date": local_date,
            "date_run": order.get("_legacy_date_run") or today_vn().isoformat(),
            "raw_json": dump(order), "synced_at": now_iso(), "history": history}


def ingest_orders(db, orders, shop_id, start, end, *, origin="api", price_rules=None):
    """Atomic upsert. Preserve verified contact choices and opt-outs on sync."""
    with db:
        begin_write(db)
        deleted = {r[0] for r in db.execute("SELECT order_id FROM app_deleted_orders WHERE shop_id=?",(str(shop_id),))}
        normalized = [(o, normalize_order(o, shop_id)) for o in orders if str(o.get("id")) not in deleted]
        rules = price_rules or {}
        rules_hash = price_rules_hash(rules)
        for raw, o in normalized:
            upsert(db,"customers",{**{col:o[col] for col in
                ("customer_key","shop_id","display_name","phone","username_candidate")},"updated_at":o["synced_at"]},
                ["customer_key"],["display_name","phone","username_candidate","updated_at"])
            cols = ("shop_id","order_id","customer_key","status","source","amount","amount_source",
                    "created_at","updated_at","status_at","update_date","date_run","raw_json","synced_at")
            upsert(db,"orders",{col:o[col] for col in cols},["shop_id","order_id"],list(cols[2:]))
            for table in ("order_items", "status_events"):
                db.execute(f"DELETE FROM {table} WHERE shop_id=? AND order_id=?", (str(shop_id), o["order_id"]))
            price_items = []
            for i, item in enumerate(raw.get("items") or []):
                if not isinstance(item, dict):
                    raise ValueError("Dòng sản phẩm trong phản hồi API không phải đối tượng JSON; hủy thay đổi của đợt đồng bộ.")
                variation = item.get("variation_info") or {}
                if not isinstance(variation, dict):
                    variation = {}
                product_name = variation.get("name") or item.get("product_name") or item.get("name")
                db.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?,?,?,?)",
                           (str(shop_id),o["order_id"],i,variation.get("display_id") or variation.get("product_display_id") or item.get("sku"),
                            product_name,number(item.get("quantity")),number(item.get("price")),
                            number(item.get("total_discount")),dump(item)))
                price_items.append({"sku":variation.get("display_id") or variation.get("product_display_id") or item.get("sku"),
                    "quantity":number(item.get("quantity")),"unit_price":number(item.get("price")),
                    "total_discount":number(item.get("total_discount")),"line_no":i})
            for i, event in enumerate(o["history"]):
                db.execute("INSERT INTO status_events VALUES (?,?,?,?,?,?)",
                           (str(shop_id),o["order_id"],i,as_status(event.get("status")),
                            api_datetime(event.get("updated_at")),dump(event)))
            price = evaluate_price(o,price_items,rules)
            upsert(db,"app_order_prices",{"shop_id":str(shop_id),"order_id":o["order_id"],
                "price_eligible":int(price["eligible"]),"rules_hash":rules_hash,"updated_at":o["synced_at"]},
                ["shop_id","order_id"],["price_eligible","rules_hash","updated_at"])
        cursor = db.execute("INSERT INTO sync_runs(shop_id,started_at,start_date,end_date,row_count,origin) VALUES (?,?,?,?,?,?)",
                            (str(shop_id),now_iso(),str(start),str(end),len(normalized),origin))
        db.executemany("INSERT INTO sync_run_orders VALUES (?,?,?,?)",
                       [(cursor.lastrowid,str(shop_id),o["order_id"],i) for i,(_,o) in enumerate(normalized)])
    return len(normalized)


def _legacy_list(value):
    if not value:
        return []
    result = ast.literal_eval(value)
    if not isinstance(result, list):
        raise ValueError("Cột SKU và cột số lượng trong CSV phải chứa danh sách.")
    return result


def import_legacy_csv(db, path, shop_id):
    """Migration only. Historical CSV cannot reconstruct discarded API fields."""
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    orders = []
    for row in rows:
        if not row.get("order_id"):
            raise ValueError("CSV có dòng thiếu order_id.")
        skus, quantities = _legacy_list(row.get("Sku")), _legacy_list(row.get("SoLuong"))
        items = [{"variation_info":{"display_id":sku},
                  "quantity":quantities[i] if i < len(quantities) else None} for i, sku in enumerate(skus)]
        # Never replace a full API record with a lossy historical CSV record.
        if db.execute("SELECT 1 FROM orders WHERE shop_id=? AND order_id=?", (str(shop_id),row["order_id"])).fetchone():
            continue
        orders.append({"id":row["order_id"],"bill_full_name":row.get("user"),
                       "bill_phone_number":row.get("phone"),"items":items,
                       "status":row.get("status"),"order_sources_name":row.get("order_sources_name"),
                       "buyer_total_amount":row.get("buyer_total_amount"),
                       "_legacy_update_date":row.get("update_date"),"_legacy_date_run":row.get("date_run"),
                       "_legacy_row":row})
    if not orders:
        return 0  # Rerunning migration must not replace the current sync batch.
    return ingest_orders(db,orders,shop_id,"legacy","legacy",origin="legacy_csv")


def latest_sync_id(db, shop_id=None):
    row = db.execute("""SELECT id FROM sync_runs WHERE (? IS NULL OR shop_id=?)
                     AND (origin!='legacy_csv' OR row_count>0) ORDER BY id DESC LIMIT 1""",
                     (None if shop_id is None else str(shop_id),None if shop_id is None else str(shop_id))).fetchone()
    return row["id"] if row else None


def batch_orders(db, *, shop_id=None, sync_run_id=None):
    """The current batch replaces the old CSV handoff; DB history stays separate."""
    if sync_run_id is None:
        sync_run_id = latest_sync_id(db,shop_id)
    return [dict(r) for r in db.execute("""SELECT o.* FROM orders o JOIN sync_run_orders s
        USING(shop_id,order_id) WHERE s.sync_run_id=? AND (? IS NULL OR o.shop_id=?)
        ORDER BY s.position""", (sync_run_id,None if shop_id is None else str(shop_id),
                                 None if shop_id is None else str(shop_id)))]


def resolved_username(contact, order, *, require_verified=False):
    if contact["verified"] and contact["username"]:
        return contact["username"]
    if require_verified:
        return None
    # Read from THIS ORDER, never the last profile name stored for a customer
    # who may have multiple orders / TikTok accounts on the same phone number.
    return username_candidate(json.loads(order["raw_json"]).get("bill_full_name"))


def age_filter(order, min_days, max_days, today):
    if min_days is None and max_days is None:
        return None
    try:
        age = (today-date.fromisoformat(order["update_date"])).days
    except (ValueError,TypeError):
        return "missing_date"
    if (min_days is not None and age < min_days) or (max_days is not None and age > max_days):
        return "outside_date_range"
    return None


def refresh_business_fields(db):
    """Recompute derived columns from saved raw data; leave CSV values intact."""
    changed = 0
    with db:
        for row in db.execute("SELECT * FROM orders").fetchall():
            raw = json.loads(row["raw_json"])
            o = normalize_order(raw,row["shop_id"])
            values = (o["status"],o["status_at"],o["update_date"],o["amount"],o["amount_source"])
            if values != tuple(row[k] for k in ("status","status_at","update_date","amount","amount_source")):
                db.execute("UPDATE orders SET status=?,status_at=?,update_date=?,amount=?,amount_source=? WHERE shop_id=? AND order_id=?",
                           (*values,row["shop_id"],row["order_id"]))
                db.execute("DELETE FROM app_order_prices WHERE shop_id=? AND order_id=?",(row["shop_id"],row["order_id"]))
                changed += 1
    return changed


def write_csv(path, rows, fields=None):
    rows = [dict(row) for row in rows]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0]) if rows else [])
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def export_contacts(db, path):
    return write_csv(path, db.execute("""SELECT customer_key,display_name,phone,username_candidate,
          COALESCE(username,'') AS username,verified,opted_out FROM customers ORDER BY customer_key"""),
          ["customer_key","display_name","phone","username_candidate","username","verified","opted_out"])


def import_contacts(db, path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    with db:
        for row in rows:
            if row.get("verified") not in {"0", "1"} or row.get("opted_out") not in {"0", "1"}:
                raise ValueError("verified và opted_out chỉ nhận 0 hoặc 1.")
            raw_user = row.get("username", "").strip()
            user = username_candidate(raw_user)
            if (raw_user and not user) or (row["verified"] == "1" and not user):
                raise ValueError("Tên người dùng TikTok không hợp lệ. Hãy kiểm tra bảng liên hệ.")
            cursor = db.execute("UPDATE customers SET username=?,verified=?,opted_out=?,updated_at=? WHERE customer_key=?",
                                (user,int(row["verified"]),int(row["opted_out"]),now_iso(),row["customer_key"]))
            if cursor.rowcount != 1:
                raise ValueError("Mã khách hàng customer_key không tồn tại; hủy cập nhật bảng liên hệ.")
    return len(rows)


DEFAULT_TEMPLATE = (
    "Dạ shop chào ạ! ❤️ Shop bên Macca Ánh Dương đây ạ. "
    "Macca đợt trước mình dùng thấy ngon và chất lượng ổn không ạ? 🥰"
)
PERSONALIZED_TEMPLATE = (
    "Dạ, shop chào mình ạ! ❤️ Shop Macca Ánh Dương đây ạ. "
    "{thanks} Mình dùng {products} trong đơn gần đây thấy ngon và chất lượng ổn không ạ? 🥰"
)


def render_message(db, order, template=DEFAULT_TEMPLATE, vip_threshold=1000000):
    items = list(db.execute("SELECT * FROM order_items WHERE shop_id=? AND order_id=? ORDER BY line_no",
                            (order["shop_id"],order["order_id"])))
    stats = db.execute("SELECT * FROM customer_stats WHERE customer_key=?", (order["customer_key"],)).fetchone()
    # Only status-3 observed orders drive repeat/VIP messaging; no claim of lifetime revenue.
    value = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE customer_key=? AND status=3",
                       (order["customer_key"],)).fetchone()[0]
    repeat = stats["status3_orders"] > 1
    segment = "vip" if value >= vip_threshold else ("returning" if repeat else "first_observed")
    names = list(dict.fromkeys(str(i["product_name"]) for i in items if i["product_name"]))
    products = ", ".join(names[:2]) if names else "mắc ca"
    context = dict(order)
    context.update(products=products, skus=", ".join(str(i["sku"]) for i in items if i["sku"]),
                   quantity=sum(i["quantity"] or 0 for i in items),
                   amount_vnd=f"{order['amount']:,.0f}đ" if order["amount"] is not None else "",
                   order_count=stats["order_count"], segment=segment,
                   thanks="Cảm ơn mình đã tiếp tục ủng hộ shop ạ." if repeat else "Cảm ơn mình đã ủng hộ shop ạ.")
    body = template.format_map(context).strip()
    if not body or len(body) > 2000:
        raise ValueError("Nội dung phải có từ 1 đến 2.000 ký tự.")
    return body, context


def recipient_block(db, username, cooldown_days=0, *, exclude_id=None):
    # Opt-out is global for a known username, even when two customer ids share it.
    if db.execute("""SELECT 1 FROM customers WHERE opted_out=1 AND
        (LOWER(username)=LOWER(?) OR LOWER(username_candidate)=LOWER(?))""", (username,username)).fetchone():
        return "opted_out"
    if db.execute("SELECT 1 FROM messages WHERE LOWER(username)=LOWER(?) AND state IN ('sending','uncertain') AND id!=?",
                  (username,exclude_id or -1)).fetchone():
        return "needs_review"
    since = (datetime.now(UTC)-timedelta(days=cooldown_days)).isoformat(timespec="seconds")
    if cooldown_days and db.execute("""SELECT 1 FROM messages WHERE LOWER(username)=LOWER(?)
       AND state IN ('submitted','sent') AND attempted_at>=? AND id!=?""",
                  (username,since,exclude_id or -1)).fetchone():
        return "cooldown"
    return None


def prepare_messages(db, *, campaign="aftercare_v1", shop_id=None, min_days=None, max_days=None,
                     cooldown_days=0, statuses=(3,), source="Tiktok", template=DEFAULT_TEMPLATE,
                     vip_threshold=1000000, today=None, require_verified=False, sync_run_id=None,
                     order_keys=None):
    """Original batch/status/source/name rules, plus persistent duplicate tracking."""
    if ((min_days is not None and min_days < 0) or (max_days is not None and max_days < 0)
        or (min_days is not None and max_days is not None and max_days < min_days)
        or cooldown_days < 0 or not campaign.strip()):
        raise ValueError("Bộ lọc chiến dịch không hợp lệ.")
    today = today or today_vn()
    sync_run_id = sync_run_id if sync_run_id is not None else latest_sync_id(db,shop_id)
    if order_keys is None:
        orders = batch_orders(db,shop_id=shop_id,sync_run_id=sync_run_id)
    else:
        # Application mode explicitly selects retained eligible orders across
        # days 1–15. Notebook default remains the latest sync batch.
        orders = []
        for key in order_keys:
            row = db.execute("SELECT * FROM orders WHERE shop_id=? AND order_id=?",tuple(key)).fetchone()
            if row:
                orders.append(dict(row))
    report, previews, handled = Counter(), [], set()
    with db:
        for row in orders:
            o = dict(row)
            if shop_id is not None and o["shop_id"] != str(shop_id):
                continue
            if o["status"] not in statuses or o["source"] != source:
                report["status_or_source"] += 1
                continue
            age_block = age_filter(o,min_days,max_days,today)
            if age_block:
                report[age_block] += 1
                continue
            customer = db.execute("SELECT * FROM customers WHERE customer_key=?", (o["customer_key"],)).fetchone()
            if customer["opted_out"]:
                report["opted_out"] += 1
                continue
            username = resolved_username(customer,o,require_verified=require_verified)
            if not username:
                report["unverified_username" if require_verified else "invalid_username"] += 1
                continue
            if username in handled:
                report["duplicate_recipient"] += 1
                continue
            handled.add(username)
            block = recipient_block(db,username,cooldown_days)
            if block:
                report[block] += 1
                continue
            body, context = render_message(db,o,template,vip_threshold)
            existing = db.execute("""SELECT id,state FROM messages
                WHERE campaign=? AND shop_id=? AND order_id=? AND username=?""",
                (campaign,o["shop_id"],o["order_id"],username)).fetchone()
            if existing:
                report[f"existing_{existing['state']}"] += 1
                continue
            cursor = db.execute("""INSERT INTO messages(campaign,shop_id,order_id,customer_key,username,body,created_at,require_verified,sync_run_id)
                 VALUES (?,?,?,?,?,?,?,?,?)""", (campaign,o["shop_id"],o["order_id"],o["customer_key"],username,body,now_iso(),int(require_verified),sync_run_id))
            previews.append({"message_id":cursor.lastrowid,"username":username,
                             "order_id":o["order_id"],"update_date":o["update_date"],
                             "skus":context["skus"],"quantity":context["quantity"],
                             "amount":o["amount"],"amount_source":o["amount_source"],
                             "segment":context["segment"],"body":body})
            report["queued"] += 1
    return previews, dict(report)


def pending_messages(db, campaign="aftercare_v1"):
    return [dict(r) for r in db.execute("SELECT * FROM messages WHERE campaign=? AND state='pending' ORDER BY id", (campaign,))]


def reports(db):
    """Observed history only. No assumption that status 3 means cash received."""
    order_identity = concat_sql(db,"i.shop_id","':'","i.order_id")
    queries = {
        "customers": "SELECT * FROM customer_stats ORDER BY observed_order_value DESC",
        "products_status3": f"""SELECT i.sku,i.product_name,COUNT(DISTINCT {order_identity}) AS orders,
            SUM(i.quantity) AS quantity,SUM(i.total_discount) AS item_discount,
            SUM(CASE WHEN i.unit_price IS NOT NULL AND i.quantity IS NOT NULL
                THEN i.unit_price*i.quantity END) AS listed_gross_value
            FROM order_items i JOIN orders o USING(shop_id,order_id) WHERE o.status=3
            GROUP BY i.sku,i.product_name ORDER BY quantity DESC""",
        "orders_by_status_source": """SELECT status,source,COUNT(*) AS orders,
            SUM(amount) AS observed_order_value,SUM(amount IS NULL) AS missing_amount
            FROM orders GROUP BY status,source""",
        "orders_by_day": """SELECT update_date,status,COUNT(*) AS orders,SUM(amount) AS observed_order_value
            FROM orders GROUP BY update_date,status ORDER BY update_date DESC""",
        "messages": "SELECT campaign,state,COUNT(*) AS messages FROM messages GROUP BY campaign,state",
        "quality": f"""SELECT COUNT(*) AS orders,SUM(status IS NULL) AS missing_status,
            SUM(update_date IS NULL) AS missing_date,SUM(amount IS NULL) AS missing_amount,
            SUM(amount_source='total_price') AS fallback_amount,
            SUM({json_present_sql(db,'raw_json','$._legacy_row')}) AS legacy_rows
            FROM orders""",
    }
    return {name:[dict(r) for r in db.execute(sql)] for name,sql in queries.items()}


def field_inventory(db):
    """Discover every returned JSON path (including new API fields), without PII values."""
    counts, types = Counter(), {}
    def walk(value, path, seen):
        seen.add(path)
        types.setdefault(path,set()).add(type(value).__name__)
        if isinstance(value,dict):
            for key,child in value.items():
                walk(child,f"{path}.{key}",seen)
        elif isinstance(value,list):
            for child in value:
                walk(child,f"{path}[]",seen)
    for row in db.execute("SELECT raw_json FROM orders"):
        seen = set()
        walk(json.loads(row[0]),"$",seen)
        counts.update(seen)
    return [{"path":path,"orders_present":counts[path],"types":", ".join(sorted(types[path]))}
            for path in sorted(counts)]


def export_raw_orders(db, path):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8") as f:
        for row in db.execute("SELECT shop_id,order_id,raw_json FROM orders ORDER BY shop_id,order_id"):
            f.write(dump({"shop_id":row["shop_id"],"order_id":row["order_id"],"data":json.loads(row["raw_json"])})+"\n")
    return path


def _event(db, mid, state, note=None):
    db.execute("INSERT INTO message_events(message_id,at,state,note) VALUES (?,?,?,?)", (mid,now_iso(),state,note))


def claim_message(db, mid, *, daily_limit=None, cooldown_days=0, min_days=None, max_days=None,
                  statuses=(3,), source="Tiktok", today=None, enforce_current_batch=True,
                  eligibility_check=None):
    """Serialize claims across processes; recheck contact/order before pressing Enter."""
    if ((daily_limit is not None and daily_limit < 1) or cooldown_days < 0
        or (min_days is not None and min_days < 0) or (max_days is not None and max_days < 0)
        or (min_days is not None and max_days is not None and max_days < min_days)):
        raise ValueError("Giới hạn gửi không hợp lệ.")
    today = today or today_vn()
    if db.in_transaction:
        raise RuntimeError("Hãy lưu giao dịch cơ sở dữ liệu hiện tại bằng db.commit() trước khi gửi tin.")
    begin_write(db)
    try:
        message = db.execute("SELECT * FROM messages WHERE id=? AND state='pending'", (mid,)).fetchone()
        if not message:
            db.rollback()
            return None, "not_pending"
        contact = db.execute("SELECT * FROM customers WHERE customer_key=?", (message["customer_key"],)).fetchone()
        order = db.execute("SELECT * FROM orders WHERE shop_id=? AND order_id=?", (message["shop_id"],message["order_id"])).fetchone()
        reason = None
        if not contact or not order:
            reason = "order_or_contact_removed"
        elif resolved_username(contact,order,require_verified=bool(message["require_verified"])) != message["username"] or contact["opted_out"]:
            reason = "contact_changed"
        elif order["customer_key"] != message["customer_key"] or order["status"] not in statuses or order["source"] != source:
            reason = "order_changed"
        else:
            reason = age_filter(order,min_days,max_days,today)
        if not reason and eligibility_check is not None:
            reason = eligibility_check(db,dict(order))
        if not reason and enforce_current_batch:
            current_run = latest_sync_id(db,message["shop_id"])
            if not db.execute("SELECT 1 FROM sync_run_orders WHERE sync_run_id=? AND shop_id=? AND order_id=?",
                              (current_run,message["shop_id"],message["order_id"])).fetchone():
                reason = "outside_current_batch"
        reason = reason or recipient_block(db,message["username"],cooldown_days,exclude_id=mid)
        if reason:
            # Leave cooldown pending; cancel invalid/stale recipients so they are visible.
            if reason not in {"cooldown", "needs_review"}:
                db.execute("UPDATE messages SET state='cancelled',finished_at=?,error=? WHERE id=?", (now_iso(),reason,mid))
                _event(db,mid,"cancelled",reason)
            db.commit()
            return None, reason
        begin = datetime.combine(today,datetime.min.time(),VN).astimezone(UTC).isoformat(timespec="seconds")
        finish = datetime.combine(today+timedelta(days=1),datetime.min.time(),VN).astimezone(UTC).isoformat(timespec="seconds")
        attempts = db.execute("SELECT COUNT(*) FROM message_events WHERE state='sending' AND at>=? AND at<?", (begin,finish)).fetchone()[0]
        if daily_limit is not None and attempts >= daily_limit:
            db.rollback()
            return None, "daily_limit"
        stamp = now_iso()
        db.execute("UPDATE messages SET state='sending',attempted_at=? WHERE id=?", (stamp,mid))
        _event(db,mid,"sending")
        db.commit()
        return dict(message), None
    except BaseException:
        db.rollback()
        raise


def finish_message(db, mid, state, note=None):
    if state not in {"submitted", "failed", "uncertain"}:
        raise ValueError("Trạng thái kết thúc không hợp lệ.")
    with db:
        cursor = db.execute("UPDATE messages SET state=?,finished_at=?,error=? WHERE id=? AND state='sending'",
                            (state,now_iso(),note,mid))
        if cursor.rowcount != 1:
            raise ValueError("Tin nhắn không ở trạng thái đang gửi (sending).")
        _event(db,mid,state,note)


def requeue_missing_account(db, mid):
    """Return a claimed message to pending when no send was attempted."""
    with db:
        cursor = db.execute("""UPDATE messages SET state='pending',finished_at=NULL,
            error='account_not_found' WHERE id=? AND state='sending'""",(mid,))
        if cursor.rowcount != 1:
            raise ValueError("Tin nhắn không ở trạng thái đang gửi (sending).")
        _event(db,mid,"pending","account_not_found")


def review_message(db, mid, *, action, note):
    """Manual reconciliation only. No automatic retry of ambiguous sends."""
    if not note.strip() or action not in {"confirmed_sent", "confirmed_not_sent", "cancel"}:
        raise ValueError("Hãy chọn thao tác action hợp lệ và ghi chú kết quả đối chiếu trên TikTok.")
    state = {"confirmed_sent":"sent", "confirmed_not_sent":"pending", "cancel":"cancelled"}[action]
    with db:
        row = db.execute("SELECT state FROM messages WHERE id=?",(mid,)).fetchone()
        if not row or row["state"] == "sent":
            raise ValueError("Không tìm thấy tin để đối chiếu hoặc tin đã được xác nhận là đã gửi (sent).")
        # A 'sending' row may belong to a live worker. Stop all workers first.
        db.execute("UPDATE messages SET state=?,error=?,finished_at=? WHERE id=?",(state,note,now_iso(),mid))
        _event(db,mid,state,note)


def export_legacy_csv(db, path=ROOT / "output" / "HONG_User.csv", *, shop_id=None, sync_run_id=None):
    rows = []
    for o in batch_orders(db,shop_id=shop_id,sync_run_id=sync_run_id):
        if o["status"] != 3 or o["source"] != "Tiktok":
            continue
        items = list(db.execute("SELECT sku,quantity FROM order_items WHERE shop_id=? AND order_id=? ORDER BY line_no",(o["shop_id"],o["order_id"])))
        raw = json.loads(o["raw_json"])
        rows.append(dict(order_id=o["order_id"],user=raw.get("bill_full_name"),phone=str(raw.get("bill_phone_number")),
                         Sku=repr([i["sku"] for i in items]),order_sources_name=o["source"],
                         SoLuong=repr([i["quantity"] for i in items]),buyer_total_amount=o["amount"],
                         status=o["status"],date_run=o["date_run"],update_date=o["update_date"]))
    return write_csv(path,rows,["order_id","user","phone","Sku","order_sources_name","SoLuong",
                                "buyer_total_amount","status","date_run","update_date"])
