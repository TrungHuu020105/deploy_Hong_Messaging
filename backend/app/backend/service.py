"""MySQL-backed local application, scheduler and TikTok send controller."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from backend.domain import GRADE_LABELS, PRICE_TABLE, REASONS, TIERS, evaluate_price, expand_items
from hong_core import (ROOT, UTC, VN, DEFAULT_TEMPLATE, connect_db, fetch_orders,
                       ingest_orders, load_settings, normalize_order, now_iso,
                       prepare_messages, resolved_username, review_message, today_vn,
                       username_candidate)
from hong_tiktok import TikTokSender, open_browser, run_queue
from hong_database import (MYSQL_KEYS, begin_write, insert_if_absent, is_mysql,
                           load_environment, mysql_settings, upsert)

DEFAULT_RULES = {
    "prices": PRICE_TABLE, "packaging": ["HN", "HG", "TZ"],
    "packaging_multipliers": {"HN": 1, "HG": 1.1, "TZ": 1.1},
    "tier_mode": "floor", "match_mode": "all", "exclude_mixed": True,
}
DEFAULT_SCHEDULE = {
    "enabled": False, "interval_hours": 3, "batch_size": 10,
    "repeat": True, "live": False, "delay_seconds": 10,
    "min_day": 1, "max_day": 15, "next_run_at": None,
    "last_run_at": None, "last_error": None,
}
APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_schedule (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_jobs (
 id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,
 source TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL,
 requested INTEGER NOT NULL DEFAULT 0,submitted INTEGER NOT NULL DEFAULT 0,
 summary TEXT NOT NULL DEFAULT '{}',error TEXT
);
CREATE TABLE IF NOT EXISTS app_opt_out_hash (digest TEXT PRIMARY KEY,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_customer_profiles (
 key TEXT PRIMARY KEY,username TEXT,display_name TEXT,phone TEXT,
 tags TEXT NOT NULL,customer_keys TEXT NOT NULL,updated_at TEXT NOT NULL
);
"""


def iso_utc(value):
    return datetime.fromisoformat(value).astimezone(UTC)


class AppService:
    def __init__(self, db_path=None, cookie_file=ROOT / "cookies_tiktokTH.json",
                 sender_factory=TikTokSender, browser_factory=open_browser, auto_sync_enabled=False,
                 mysql_config=None, initialize=True, mode="admin"):
        if mode not in {"admin","web"}:
            raise ValueError("APP_MODE phải là admin hoặc web.")
        self.mode = mode
        self.db_path = Path(db_path) if db_path is not None else None
        self.mysql_config = dict(mysql_config) if mysql_config is not None else None
        self._db_initialized = False
        self._server_lock = None
        self._server_lock_guard = threading.Lock()
        self.cookie_file = Path(cookie_file)
        self.sender_factory = sender_factory
        self.browser_factory = browser_factory
        self.auto_sync_enabled = auto_sync_enabled
        self.auto_sync_error = None
        self._next_auto_sync_at = 0.0
        self._sync_lock = threading.Lock()
        self.driver = None
        self.account_checked = False
        self.browser_error = None
        self._stop = threading.Event()
        self._thread = None
        self._job_thread = None
        self._job_lock = threading.RLock()
        self._file_lock = None
        self._last_purge = 0.0
        if initialize:
            self.init_db()

    @contextmanager
    def db(self):
        db = connect_db(self.db_path,settings=self.mysql_config,initialize=not self._db_initialized)
        try:
            if not is_mysql(db):
                db.executescript(APP_SCHEMA)
            yield db
        finally:
            db.close()

    def init_db(self):
        if self.db_path is None and self.mysql_config is None:
            values = load_environment()
            mysql_settings(values)
            # All worker connections must keep using the database whose backend
            # lock we hold, even if .env is edited while this process is running.
            self.mysql_config = {key:values.get(key,"") for key in MYSQL_KEYS}
        with self.db() as db, db:
            insert_if_absent(db,"app_meta",{"key":"secret","value":os.urandom(32).hex()},["key"])
            insert_if_absent(db,"app_schedule",{"id":1,"payload":json.dumps(DEFAULT_SCHEDULE)},["id"])
        self._db_initialized = True

    def _lock_file(self):
        if self.db_path is None:
            db = connect_db(settings=self.mysql_config,initialize=False)
            if not db.acquire_lock(db.lock_name(self._backend_lock_purpose)):
                db.close()
                raise RuntimeError("Database MySQL này đã có một backend đang chạy.")
            self._server_lock = db
            return
        path = self.db_path.parent / ("hong_web.lock" if self.mode == "web" else "hong_app.lock")
        path.parent.mkdir(parents=True,exist_ok=True)
        file = path.open("a+b")
        try:
            if os.fstat(file.fileno()).st_size == 0:
                file.write(b"1")
                file.flush()
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            file.close()
            raise RuntimeError("Ứng dụng đã chạy với cơ sở dữ liệu này. Chỉ mở một backend.") from None
        self._file_lock = file

    @property
    def _backend_lock_purpose(self):
        return "web" if self.mode == "web" else "backend"

    def _require_admin(self):
        if self.mode != "admin":
            raise ValueError("Chức năng gửi tin chỉ có trên máy local admin.")

    @contextmanager
    def _task_lock(self,purpose):
        if self.db_path is not None:
            yield
            return
        with self.db() as db:
            name = db.lock_name(purpose)
            if not db.acquire_lock(name):
                raise ValueError("Tác vụ đang chạy trên máy khác. Đợi hoàn tất rồi thử lại.")
            try:
                yield
            finally:
                db.release_lock(name)

    def _check_server_lock(self):
        if self.db_path is not None:
            return
        with self._server_lock_guard:
            db = self._server_lock
            if db is None:
                raise RuntimeError("Backend MySQL chưa khởi động hoặc đã mất khóa điều phối.")
            try:
                owner = db.execute("SELECT IS_USED_LOCK(?)",(db.lock_name(self._backend_lock_purpose),)).fetchone()[0]
                if owner != db.raw.thread_id():
                    raise RuntimeError("Đã mất khóa backend MySQL. Khởi động lại backend trước khi gửi.")
            except Exception:
                self._stop.set()
                raise

    def start(self):
        try:
            self._start()
        except BaseException:
            self.stop()
            raise

    def _start(self):
        if not self._db_initialized:
            self.init_db()
        self._lock_file()
        if self.mode == "admin":
            self._recover_send_jobs()
        self.purge_expired()
        self._stop.clear()
        self._thread = threading.Thread(target=self._scheduler_loop,name="hong-scheduler",daemon=True)
        self._thread.start()

    def _recover_send_jobs(self):
        with self.db() as db,db:
            # A crash may have happened after pressing Enter. Reconcile manually.
            for row in db.execute("SELECT id FROM messages WHERE state='sending'").fetchall():
                db.execute("UPDATE messages SET state='uncertain',error='interrupted' WHERE id=?",(row["id"],))
                db.execute("INSERT INTO message_events(message_id,at,state,note) VALUES (?,?,?,?)",
                           (row["id"],now_iso(),"uncertain","Backend đã khởi động lại"))
            db.execute("UPDATE app_jobs SET state='interrupted',finished_at=?,error='Backend đã khởi động lại' WHERE state IN ('queued','running')",(now_iso(),))

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._job_thread:
            self._job_thread.join()
        self.close_browser()
        with self._server_lock_guard:
            if self._server_lock:
                self._server_lock.close()
                self._server_lock = None
        if self._file_lock:
            file = self._file_lock
            self._file_lock = None
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(file.fileno(),fcntl.LOCK_UN)
            file.close()

    def get_rules(self, db=None):
        if db is None:
            with self.db() as conn:
                return self.get_rules(conn)
        row = db.execute("SELECT value FROM app_meta WHERE `key`='rules'").fetchone()
        return json.loads(row["value"]) if row else DEFAULT_RULES.copy()

    def get_schedule(self, db=None):
        if db is None:
            with self.db() as conn:
                return self.get_schedule(conn)
        row = db.execute("SELECT payload FROM app_schedule WHERE id=1").fetchone()
        return json.loads(row["payload"])

    def save_schedule(self, data):
        self._require_admin()
        schedule = self.get_schedule()
        for field in ("enabled","interval_hours","batch_size","repeat","live","delay_seconds","min_day","max_day"):
            if field in data:
                schedule[field] = data[field]
        if not 0.25 <= schedule["interval_hours"] <= 168:
            raise ValueError("Khoảng thời gian phải từ 15 phút đến 7 ngày.")
        if not 1 <= schedule["batch_size"] <= 1000 or not 0 <= schedule["delay_seconds"] <= 3600:
            raise ValueError("Số khách hoặc thời gian nghỉ không hợp lệ.")
        if not 0 <= schedule["min_day"] <= schedule["max_day"] <= 15:
            raise ValueError("Khoảng ngày nhận phải nằm trong 0–15 ngày.")
        if schedule["enabled"] and schedule["live"] and not self.browser_ready():
            raise ValueError("Mở Chrome và kiểm tra tài khoản gửi trước khi bật lịch gửi thật.")
        if schedule["enabled"]:
            schedule["next_run_at"] = (datetime.now(UTC)+timedelta(hours=schedule["interval_hours"])).isoformat(timespec="seconds")
            schedule["last_error"] = None
        else:
            schedule["next_run_at"] = None
        with self.db() as db,db:
            db.execute("UPDATE app_schedule SET payload=? WHERE id=1",(json.dumps(schedule,ensure_ascii=False),))
        return schedule

    def browser_ready(self):
        return self.driver is not None and self.account_checked

    def browser_state(self):
        return {"open":self.driver is not None,"account_checked":self.account_checked,
                "ready":self.browser_ready(),"error":self.browser_error}

    def open_browser(self):
        self._require_admin()
        with self._job_lock:
            if self._job_thread and self._job_thread.is_alive():
                raise ValueError("Đợi đợt gửi hiện tại kết thúc trước khi mở lại Chrome.")
            self.close_browser()
            self.driver = self.browser_factory(self.cookie_file)
            self.account_checked = False
            self.browser_error = None
            return self.browser_state()

    def confirm_browser(self):
        self._require_admin()
        if self.driver is None:
            raise ValueError("Chưa mở Chrome.")
        self.account_checked = True
        return self.browser_state()

    def close_browser(self):
        if self._job_thread and self._job_thread.is_alive():
            raise ValueError("Đợi đợt gửi hiện tại kết thúc trước khi đóng Chrome.")
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None
        self.account_checked = False
        return self.browser_state()

    def _hash_username(self, db, username):
        secret = bytes.fromhex(db.execute("SELECT value FROM app_meta WHERE `key`='secret'").fetchone()[0])
        return hmac.new(secret,username.casefold().encode("utf-8"),hashlib.sha256).hexdigest()

    def _denied(self, db, username):
        if not username:
            return False
        digest = self._hash_username(db,username)
        return db.execute("SELECT 1 FROM app_opt_out_hash WHERE digest=?",(digest,)).fetchone() is not None

    def set_opt_out(self, customer_key, value):
        with self.db() as db,db:
            row = db.execute("SELECT username,username_candidate FROM customers WHERE customer_key=?",(customer_key,)).fetchone()
            if row is None:
                raise ValueError("Không tìm thấy khách hàng.")
            db.execute("UPDATE customers SET opted_out=?,updated_at=? WHERE customer_key=?",(int(value),now_iso(),customer_key))
            for username in {row["username"],row["username_candidate"]} - {None,""}:
                digest = self._hash_username(db,username)
                if value:
                    insert_if_absent(db,"app_opt_out_hash",{"digest":digest,"created_at":now_iso()},["digest"])
                else:
                    db.execute("DELETE FROM app_opt_out_hash WHERE digest=?",(digest,))
        return {"customer_key":customer_key,"opted_out":value}

    @staticmethod
    def _profile_key(order):
        return order["username"].casefold() if order["username"] else f"order:{order['shop_id']}:{order['order_id']}"

    def _profiles(self, db):
        return {r["key"]:{**dict(r),"tags":json.loads(r["tags"]),
                          "customer_keys":json.loads(r["customer_keys"])}
                for r in db.execute("SELECT * FROM app_customer_profiles")}

    @staticmethod
    def _delete_order(db, shop_id, order_id):
        args = (shop_id,order_id)
        db.execute("""DELETE FROM message_events WHERE message_id IN
            (SELECT id FROM messages WHERE shop_id=? AND order_id=?)""",args)
        db.execute("DELETE FROM messages WHERE shop_id=? AND order_id=?",args)
        db.execute("DELETE FROM sync_run_orders WHERE shop_id=? AND order_id=?",args)
        db.execute("DELETE FROM orders WHERE shop_id=? AND order_id=?",args)

    def _clean_orphan_customers(self, db):
        protected = {key for profile in self._profiles(db).values() for key in profile["customer_keys"]}
        orphaned = db.execute("""SELECT * FROM customers c WHERE NOT EXISTS
            (SELECT 1 FROM orders o WHERE o.customer_key=c.customer_key)""").fetchall()
        for customer in orphaned:
            if customer["customer_key"] in protected:
                continue
            if customer["opted_out"]:
                for username in {customer["username"],customer["username_candidate"]} - {None,""}:
                    insert_if_absent(db,"app_opt_out_hash",
                        {"digest":self._hash_username(db,username),"created_at":now_iso()},["digest"])
            db.execute("DELETE FROM customers WHERE customer_key=?",(customer["customer_key"],))

    def save_customer_profile(self, key, tags, delete_orders):
        clean_tags = []
        for tag in tags:
            tag = " ".join(tag.split())
            if not tag or len(tag) > 80:
                raise ValueError("Tên thẻ phải có từ 1 đến 80 ký tự.")
            if tag.casefold() not in {t.casefold() for t in clean_tags}:
                clean_tags.append(tag)
        if len(clean_tags) > 20:
            raise ValueError("Mỗi khách có tối đa 20 thẻ.")
        # Serialize with sync and job creation, and commit tags/deletions together.
        with self._sync_lock,self._job_lock,self.db() as db,db:
            if delete_orders and self._job_thread and self._job_thread.is_alive():
                raise ValueError("Đang chạy đợt gửi. Đợi hoàn tất trước khi xóa đơn.")
            begin_write(db)
            rows = []
            for row in db.execute("SELECT * FROM orders").fetchall():
                snapshot = self._snapshot(db,row)
                if self._profile_key({**dict(row),"username":snapshot["username"]}) == key:
                    rows.append((dict(row),snapshot))
            previous = self._profiles(db).get(key)
            if not rows and not previous:
                raise ValueError("Hồ sơ không còn tồn tại. Hãy làm mới danh sách.")
            owned = {(r["shop_id"],r["order_id"]) for r,_ in rows}
            requested = {(r["shop_id"],r["order_id"]) for r in delete_orders}
            if not requested <= owned:
                raise ValueError("Đơn cần xóa không còn tồn tại hoặc không thuộc hồ sơ này. Hãy mở lại hồ sơ.")
            for shop_id,order_id in requested:
                if self._order_has_active_message(db,shop_id,order_id):
                    raise ValueError("Đơn có tin đang gửi hoặc chưa rõ kết quả trên máy local. Đối chiếu trước khi xóa.")
            if clean_tags:
                row,snapshot = rows[0] if rows else (None,None)
                customer_keys = sorted({r["customer_key"] for r,_ in rows} |
                                       set(previous["customer_keys"] if previous else []))
                upsert(db,"app_customer_profiles",{
                    "key":key,"username":snapshot["username"] if snapshot else previous["username"],
                    "display_name":snapshot["raw"].get("bill_full_name") if snapshot else previous["display_name"],
                    "phone":snapshot["raw"].get("bill_phone_number") if snapshot else previous["phone"],
                    "tags":json.dumps(clean_tags,ensure_ascii=False),"customer_keys":json.dumps(customer_keys),
                    "updated_at":now_iso()},["key"],["username","display_name","phone","tags","customer_keys","updated_at"])
            else:
                db.execute("DELETE FROM app_customer_profiles WHERE `key`=?",(key,))
            for shop_id,order_id in requested:
                insert_if_absent(db,"app_deleted_orders",
                    {"shop_id":shop_id,"order_id":order_id,"deleted_at":now_iso()},["shop_id","order_id"])
                self._delete_order(db,shop_id,order_id)
            self._clean_orphan_customers(db)
        return {"key":key,"tags":clean_tags,"deleted_orders":len(requested)}

    def _snapshot(self, db, order):
        order = dict(order)
        customer = db.execute("SELECT * FROM customers WHERE customer_key=?",(order["customer_key"],)).fetchone()
        items = [dict(r) for r in db.execute("SELECT * FROM order_items WHERE shop_id=? AND order_id=? ORDER BY line_no",
                                             (order["shop_id"],order["order_id"]))]
        raw = json.loads(order["raw_json"])
        user = resolved_username(customer,order) if customer else username_candidate(raw.get("bill_full_name"))
        return {"order":order,"customer":dict(customer) if customer else None,"raw":raw,
                "username":user,"items":items}

    def assess(self, db, snapshot, *, rules=None, today=None):
        rules = rules or self.get_rules(db)
        today = today or today_vn()
        order = snapshot["order"]
        product = evaluate_price(order,snapshot["items"],rules)
        received = order["update_date"] if order["status"] == 3 else None
        try:
            age = (today-date.fromisoformat(received)).days if received else None
        except ValueError:
            age = None
        reason = product["reason"]
        if order["status"] != 3:
            reason = "not_received"
        elif order["source"] != "Tiktok":
            reason = "wrong_source"
        elif age is None:
            reason = "missing_date"
        elif age > 15:
            reason = "expired"
        elif not snapshot["username"]:
            reason = "invalid_username"
        elif snapshot["customer"] and snapshot["customer"]["opted_out"] or self._denied(db,snapshot["username"]):
            reason = "opted_out"
        return {**product,"price_eligible":product["eligible"],"price_reason":product["reason"],
                "price_reason_label":REASONS.get(product["reason"],product["reason"]),
                "eligible":reason == "eligible","reason":reason,"reason_label":REASONS.get(reason,reason),
                "received_date":received,"age_days":age}

    def _status_for_username(self, db, username):
        if not username:
            return "not_sent"
        states = [r[0] for r in db.execute("SELECT state FROM messages WHERE LOWER(username)=LOWER(?) ORDER BY id DESC",(username,))]
        if any(s in {"submitted","sent"} for s in states):
            return "sent"
        if any(s in {"sending","uncertain"} for s in states):
            return "needs_review"
        if any(s == "pending" for s in states):
            return "pending"
        if any(s == "failed" for s in states):
            return "failed"
        return "not_sent"

    def orders(self, *, include_excluded=True):
        with self.db() as db:
            rules = self.get_rules(db)
            today = today_vn()
            rows = []
            for order in db.execute("SELECT * FROM orders ORDER BY update_date DESC,order_id DESC").fetchall():
                s = self._snapshot(db,order)
                assessment = self.assess(db,s,rules=rules,today=today)
                if not include_excluded and not assessment["eligible"]:
                    continue
                rows.append({"shop_id":order["shop_id"],"order_id":order["order_id"],
                             "customer_key":order["customer_key"],"username":s["username"],
                             "display_name":s["raw"].get("bill_full_name"),
                             "phone":s["raw"].get("bill_phone_number"),"amount":order["amount"],
                             "amount_source":order["amount_source"],"source":order["source"],
                             "status":order["status"],"items":assessment["items"],
                             "eligible":assessment["eligible"],"reason":assessment["reason"],
                             "reason_label":assessment["reason_label"],"price_checks":assessment["price_checks"],
                             "price_eligible":assessment["price_eligible"],
                             "price_reason":assessment["price_reason"],
                             "price_reason_label":assessment["price_reason_label"],
                             "threshold_amount":assessment["threshold_amount"],
                             "total_kg":assessment["total_kg"],"units_500g":assessment["units_500g"],
                             "received_date":assessment["received_date"],"age_days":assessment["age_days"],
                             "message_state":self._status_for_username(db,s["username"]),
                             "opted_out":bool(s["customer"] and s["customer"]["opted_out"])})
            return rows

    def customers(self, *, include_excluded=False):
        orders = self.orders(include_excluded=True)
        with self.db() as db:
            profiles = self._profiles(db)
        groups = {}
        for order in orders:
            key = self._profile_key(order)
            if key not in profiles and (order["status"] != 3 or order["source"] != "Tiktok" or order["age_days"] is None or not 0 <= order["age_days"] <= 15):
                continue
            if not order["eligible"] and not include_excluded:
                continue
            if key not in groups:
                groups[key] = {"key":key,"username":order["username"],"display_name":order["display_name"],
                               "phone":order["phone"],"orders":[],"total_amount":0,
                               "total_kg":0,"units_500g":0,"age_days":order["age_days"],
                               "received_date":order["received_date"],"message_state":order["message_state"],
                               "eligible":False,"customer_keys":[],"reason":order["reason"],
                               "price_eligible":False,"reason_codes":[],"latest_order_id":order["order_id"],
                               "latest_amount":order["amount"],"latest_threshold_amount":order["threshold_amount"],
                               "latest_reason":order["reason"],"latest_reason_label":order["reason_label"],
                               "latest_price_eligible":order["price_eligible"],
                               "latest_price_reason_label":order["price_reason_label"],
                                "eligible_amount":0,"eligible_order_count":0,
                                "tags":profiles.get(key,{}).get("tags",[])}
            group = groups[key]
            group["orders"].append(order)
            group["customer_keys"].append(order["customer_key"])
            group["total_amount"] += order["amount"] or 0
            group["total_kg"] += sum(i["total_kg"] or 0 for i in order["items"] if i["grade"])
            group["units_500g"] += sum(i["units_500g"] or 0 for i in order["items"] if i["grade"])
            group["price_eligible"] = group["price_eligible"] or order["price_eligible"]
            if not order["price_eligible"] and order["price_reason"] not in group["reason_codes"]:
                group["reason_codes"].append(order["price_reason"])
            if order["eligible"]:
                group["eligible_amount"] += order["amount"] or 0
                group["eligible_order_count"] += 1
                group["eligible"] = True
            if order["message_state"] == "sent":
                group["message_state"] = "sent"
            elif order["message_state"] == "needs_review" and group["message_state"] != "sent":
                group["message_state"] = "needs_review"
        if include_excluded:
            groups = {key:group for key,group in groups.items()
                      if key in profiles or any(any(i["grade"] for i in order["items"]) for order in group["orders"])}
            for key,profile in profiles.items():
                if key not in groups:
                    groups[key] = {"key":key,"username":profile["username"],"display_name":profile["display_name"],
                        "phone":profile["phone"],"orders":[],"total_amount":0,"total_kg":0,"units_500g":0,
                        "age_days":None,"received_date":None,"message_state":"not_sent","eligible":False,
                        "customer_keys":profile["customer_keys"],"tags":profile["tags"],"reason":"no_orders",
                        "price_eligible":False,"reason_codes":[],"latest_order_id":None,"latest_amount":None,
                        "latest_threshold_amount":None,"latest_reason":"no_orders","latest_reason_label":"Chưa có đơn đã lưu",
                        "latest_price_eligible":False,"latest_price_reason_label":"Chưa có đơn đã lưu",
                        "eligible_amount":0,"eligible_order_count":0}
        return sorted(groups.values(),key=lambda c: (-(c["age_days"] or 0),c["username"] is None,(c["username"] or "").casefold()))

    def overview(self):
        customers = [c for c in self.customers(include_excluded=True) if c["eligible"]]
        orders = self.orders()
        ages = Counter(c["age_days"] for c in customers if c["eligible"])
        states = Counter(c["message_state"] for c in customers)
        reasons = Counter(o["reason"] for o in orders if not o["eligible"])
        return {"today":today_vn().isoformat(),"total_customers":len(customers),
                "total_orders":sum(o["eligible"] for o in orders),
                "total_amount":sum(c["eligible_amount"] for c in customers),
                "total_kg":sum(o["total_kg"] for c in customers for o in c["orders"] if o["eligible"]),
                "day_zero":ages[0],"days":[{"day":day,"count":ages[day]} for day in range(1,16)],
                "message_states":dict(states),"excluded":dict(reasons),
                "schedule":self.get_schedule(),"browser":self.browser_state(),
                "active_job":self.active_job(),"sync":self.sync_state()}

    def sync_state(self):
        with self.db() as db:
            row = db.execute("""SELECT started_at,start_date,end_date,row_count,origin FROM sync_runs
                WHERE origin IN ('app_api','app_auto') ORDER BY id DESC LIMIT 1""").fetchone()
        return {"last_run":dict(row) if row else None,"auto_error":self.auto_sync_error,
                "automatic":self.auto_sync_enabled}

    def active_job(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM app_jobs WHERE state IN ('queued','running') ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def preview(self, batch_size=10, usernames=None, min_day=1, max_day=15):
        self._require_admin()
        selected = {username.casefold() for username in usernames} if usernames is not None else None
        customers = [c for c in self.customers() if c["eligible"] and c["message_state"] in {"not_sent","pending"}
                     and c["username"] and min_day <= c["age_days"] <= max_day
                     and (selected is None or c["username"].casefold() in selected)]
        customers = customers[:batch_size]
        return [{"username":c["username"],"display_name":c["display_name"],
                 "age_days":c["age_days"],"amount":c["eligible_amount"],
                 "order_id":c["orders"][0]["order_id"],"customer_key":c["orders"][0]["customer_key"],
                 "body":DEFAULT_TEMPLATE} for c in customers]

    def sync_orders(self, start, end, *, origin="app_api"):
        with self._sync_lock,self._task_lock("sync"):
            if origin == "app_auto":
                window = self._auto_sync_window(today_vn())
                if window is None:
                    return {"fetched":0,"saved":0,"reasons":{},"purged":0,"skipped":True}
                start,end,_ = window
                start,end = start.isoformat(),end.isoformat()
            result = self._sync_orders_impl(start,end,origin=origin)
            self.auto_sync_error = None
            return result

    def _sync_orders_impl(self, start, end, *, origin):
        settings = load_settings()
        incoming = fetch_orders(settings,start,end,page_size=100,time_filter=3,page_parameter="page")
        shop_id = settings["PANCAKE_SHOP_ID"]
        reasons = Counter()
        retained = []
        with self.db() as db:
            rules = self.get_rules(db)
            existing = {r[0] for r in db.execute("SELECT order_id FROM orders WHERE shop_id=?",(shop_id,))}
            deleted = {r[0] for r in db.execute("SELECT order_id FROM app_deleted_orders WHERE shop_id=?",(str(shop_id),))}
            for raw in incoming:
                if str(raw["id"]) in deleted:
                    reasons["manually_deleted"] += 1
                    continue
                normalized = normalize_order(raw,shop_id)
                items = []
                for i,item in enumerate(raw.get("items") or []):
                    if isinstance(item,dict):
                        variation = item.get("variation_info") or {}
                        if not isinstance(variation,dict):
                            variation = {}
                        items.append({"sku":variation.get("display_id") or variation.get("product_display_id") or item.get("sku"),
                                      "quantity":item.get("quantity"),"unit_price":item.get("price"),
                                      "total_discount":item.get("total_discount"),"line_no":i})
                assessment = evaluate_price(normalized,items,rules)
                age = None
                if normalized["update_date"]:
                    age = (today_vn()-date.fromisoformat(normalized["update_date"])).days
                if normalized["status"] != 3:
                    reason = "not_received"
                elif normalized["source"] != "Tiktok":
                    reason = "wrong_source"
                elif age is None:
                    reason = "missing_date"
                elif age > 15:
                    reason = "expired"
                else:
                    reason = assessment["reason"]
                reasons[reason] += 1
                if (normalized["status"] == 3 and normalized["source"] == "Tiktok"
                        and age is not None and 0 <= age <= 15) or str(raw["id"]) in existing:
                    retained.append(raw)
            saved = ingest_orders(db,retained,shop_id,start,end,origin=origin)
        deleted = self.purge_expired()
        return {"fetched":len(incoming),"saved":saved,"reasons":dict(reasons),"purged":deleted}

    def _auto_sync_window(self, today):
        first = today-timedelta(days=15)
        covered = set()
        with self.db() as db:
            runs = db.execute("""SELECT start_date,end_date,started_at FROM sync_runs
                WHERE origin IN ('app_api','app_auto') ORDER BY id DESC""").fetchall()
        for run in runs:
            try:
                start,end = date.fromisoformat(run["start_date"]),date.fromisoformat(run["end_date"])
            except ValueError:
                continue
            cursor = max(first,start)
            while cursor <= min(today,end):
                covered.add(cursor)
                cursor += timedelta(days=1)
        missing = [first+timedelta(days=n) for n in range(16) if first+timedelta(days=n) not in covered]
        if missing:
            return max(first,min(missing)-timedelta(days=1)),today,True
        latest = runs[0] if runs else None
        if latest:
            try:
                last = datetime.fromisoformat(latest["started_at"]).astimezone(UTC)
                if datetime.now(UTC)-last < timedelta(hours=3):
                    return None
            except ValueError:
                pass
        return today-timedelta(days=1),today,False

    def _auto_sync_if_due(self):
        if not self.auto_sync_enabled or time.monotonic() < self._next_auto_sync_at:
            return
        window = self._auto_sync_window(today_vn())
        if window is None or self._sync_lock.locked():
            return
        start,end,_ = window
        try:
            self.sync_orders(start.isoformat(),end.isoformat(),origin="app_auto")
            self.auto_sync_error = None
        except Exception as exc:
            self.auto_sync_error = str(exc)
            self._next_auto_sync_at = time.monotonic()+900

    def purge_expired(self, *, today=None):
        if self._job_thread and self._job_thread.is_alive():
            return 0  # Leave any active message intact; retry on next scheduler pass.
        today = today or today_vn()
        deleted = 0
        with self._job_lock,self.db() as db,db:
            if self._job_thread and self._job_thread.is_alive():
                return 0
            begin_write(db)
            profiles = self._profiles(db)
            protected = {key for profile in profiles.values() for key in profile["customer_keys"]}
            for row in db.execute("SELECT * FROM orders").fetchall():
                if self._order_has_active_message(db,row["shop_id"],row["order_id"]):
                    continue
                snapshot = self._snapshot(db,row)
                key = self._profile_key({**dict(row),"username":snapshot["username"]})
                if row["customer_key"] in protected or key in profiles:
                    continue
                raw = json.loads(row["raw_json"])
                status_history = raw.get("status_history") or []
                receipt_date = row["update_date"] if row["status"] == 3 else None
                if not receipt_date:
                    for event in reversed(status_history):
                        if isinstance(event,dict) and str(event.get("status")) == "3":
                            from hong_core import api_datetime
                            stamp = api_datetime(event.get("updated_at"))
                            if stamp:
                                receipt_date = datetime.fromisoformat(stamp).astimezone(VN).date().isoformat()
                                break
                if not receipt_date:
                    continue
                try:
                    age = (today-date.fromisoformat(receipt_date)).days
                except ValueError:
                    continue
                if age <= 15:
                    continue
                self._delete_order(db,row["shop_id"],row["order_id"])
                deleted += 1
            self._clean_orphan_customers(db)
        self._last_purge = time.monotonic()
        return deleted

    @staticmethod
    def _order_has_active_message(db,shop_id,order_id):
        return db.execute("""SELECT 1 FROM messages WHERE shop_id=? AND order_id=?
            AND state IN ('sending','uncertain') LIMIT 1""",(shop_id,order_id)).fetchone() is not None

    def _eligible_at_claim(self, db, order):
        assessment = self.assess(db,self._snapshot(db,order))
        return None if assessment["eligible"] else assessment["reason"]

    def create_job(self, *, source="manual", live=False, batch_size=10, min_day=1, max_day=15,
                   delay_seconds=10, usernames=None):
        self._require_admin()
        if not 1 <= batch_size <= 1000 or not 0 <= min_day <= max_day <= 15 or not 0 <= delay_seconds <= 3600:
            raise ValueError("Cấu hình đợt gửi không hợp lệ.")
        if live and not self.browser_ready():
            raise ValueError("Mở Chrome và kiểm tra tài khoản trước khi gửi thật.")
        self._check_server_lock()
        with self._job_lock:
            if self._job_thread and self._job_thread.is_alive():
                raise ValueError("Đang có một đợt gửi khác.")
            selected = self.preview(batch_size,usernames,min_day,max_day)
            with self.db() as db,db:
                cursor = db.execute("INSERT INTO app_jobs(created_at,source,mode,state,requested,summary) VALUES (?,?,?,?,?,'[]')",
                                    (now_iso(),source,"live" if live else "preview","queued",len(selected)))
                job_id = cursor.lastrowid
            kwargs = dict(job_id=job_id,live=live,selected=selected,delay_seconds=delay_seconds)
            self._job_thread = threading.Thread(target=self._execute_job,kwargs=kwargs,name=f"hong-job-{job_id}",daemon=True)
            self._job_thread.start()
            return {"id":job_id,"requested":len(selected),"mode":"live" if live else "preview"}

    def _execute_job(self, *, job_id, live, selected, delay_seconds):
        try:
            with self.db() as db,db:
                db.execute("UPDATE app_jobs SET state='running',started_at=? WHERE id=?",(now_iso(),job_id))
            results = []
            if live and selected:
                with self.db() as db:
                    keys = [(db.execute("SELECT shop_id FROM orders WHERE order_id=? AND customer_key=? ORDER BY synced_at DESC LIMIT 1",
                                        (s["order_id"],s["customer_key"])).fetchone(),s) for s in selected]
                    order_keys = [(r["shop_id"],s["order_id"]) for r,s in keys if r]
                    created,_ = prepare_messages(db,campaign="care_app_v1",order_keys=order_keys)
                    ids = [r["message_id"] for r in created]
                    for s in selected:
                        row = db.execute("""SELECT id FROM messages WHERE campaign='care_app_v1'
                            AND customer_key=? AND order_id=? AND username=? AND state='pending'
                            ORDER BY id DESC LIMIT 1""",(s["customer_key"],s["order_id"],s["username"])).fetchone()
                        if row:
                            ids.append(row["id"])
                    ids = list(dict.fromkeys(ids))
                    sender = self.sender_factory(self.driver,account_checked=True)
                    results = run_queue(db,campaign="care_app_v1",sender=sender,dry_run=False,
                                        approved_ids=ids,batch_limit=len(selected),pause_seconds=delay_seconds,
                                        enforce_current_batch=False,eligibility_check=self._eligible_at_claim,
                                        stop_event=self._stop)
            elif not live:
                results = [{"state":"preview","count":len(selected)}]
            submitted = sum(r.get("state") == "submitted" for r in results)
            attention = any(r.get("state") in {"uncertain","failed"} for r in results)
            state = "attention" if attention else "completed"
            problem = next((r for r in results if r.get("state") in {"uncertain","failed"}),None)
            issue = None
            if problem:
                issue = ("Nút trên TikTok bị cửa sổ khác che; đợt gửi đã dừng."
                         if problem.get("error") == "ElementClickInterceptedException"
                         else problem.get("detail") or f"Đợt gửi dừng vì {problem.get('error')}.")
            with self.db() as db,db:
                db.execute("UPDATE app_jobs SET state=?,finished_at=?,submitted=?,summary=?,error=? WHERE id=?",
                           (state,now_iso(),submitted,json.dumps(results,ensure_ascii=False),issue,job_id))
                if attention:
                    schedule = self.get_schedule(db)
                    schedule["enabled"] = False
                    schedule["last_error"] = "Đợt gửi dừng để kiểm tra tin nhắn hoặc đăng nhập."
                    db.execute("UPDATE app_schedule SET payload=? WHERE id=1",(json.dumps(schedule,ensure_ascii=False),))
        except BaseException as exc:
            with self.db() as db,db:
                db.execute("UPDATE app_jobs SET state='failed',finished_at=?,error=? WHERE id=?",
                           (now_iso(),type(exc).__name__,job_id))
                schedule = self.get_schedule(db)
                schedule["enabled"] = False
                schedule["last_error"] = "Đợt gửi gặp lỗi. Hãy kiểm tra lịch sử trước khi bật lại."
                db.execute("UPDATE app_schedule SET payload=? WHERE id=1",(json.dumps(schedule,ensure_ascii=False),))

    def jobs(self, limit=30):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM app_jobs ORDER BY id DESC LIMIT ?",(limit,))]

    def messages(self, limit=200):
        with self.db() as db:
            return [dict(r) for r in db.execute("""SELECT m.*,o.amount,o.update_date FROM messages m
                JOIN orders o ON o.shop_id=m.shop_id AND o.order_id=m.order_id
                ORDER BY m.id DESC LIMIT ?""",(limit,))]

    def review(self, message_id, action, note):
        self._require_admin()
        with self.db() as db:
            review_message(db,message_id,action=action,note=note)
        return {"id":message_id,"action":action}

    def _scheduler_loop(self):
        while not self._stop.wait(1):
            try:
                self._check_server_lock()
                if time.monotonic()-self._last_purge > 60:
                    self.purge_expired()
                self._auto_sync_if_due()
                if self.mode == "web":
                    continue
                schedule = self.get_schedule()
                due = schedule["enabled"] and schedule["next_run_at"] and iso_utc(schedule["next_run_at"]) <= datetime.now(UTC)
                if not due:
                    continue
                if schedule["live"] and not self.browser_ready():
                    continue  # Keep the due time; run after account is checked.
                if self._job_thread and self._job_thread.is_alive():
                    continue
                with self.db() as db,db:
                    current = self.get_schedule(db)
                    if not current["enabled"] or current["next_run_at"] != schedule["next_run_at"]:
                        continue
                    current["last_run_at"] = now_iso()
                    current["next_run_at"] = ((datetime.now(UTC)+timedelta(hours=current["interval_hours"])).isoformat(timespec="seconds")
                                              if current["repeat"] else None)
                    if not current["repeat"]:
                        current["enabled"] = False
                    db.execute("UPDATE app_schedule SET payload=? WHERE id=1",(json.dumps(current,ensure_ascii=False),))
                self.create_job(source="schedule",live=schedule["live"],batch_size=schedule["batch_size"],
                                min_day=schedule["min_day"],max_day=schedule["max_day"],
                                delay_seconds=schedule["delay_seconds"])
            except BaseException as exc:
                if self.mode == "web":
                    self.auto_sync_error = f"Đồng bộ tạm dừng: {type(exc).__name__}"
                    continue
                try:
                    with self.db() as db,db:
                        schedule = self.get_schedule(db)
                        schedule["enabled"] = False
                        schedule["last_error"] = f"Lịch tạm dừng: {type(exc).__name__}"
                        db.execute("UPDATE app_schedule SET payload=? WHERE id=1",(json.dumps(schedule,ensure_ascii=False),))
                except Exception:
                    self.auto_sync_error = "Không kết nối được database. Kiểm tra MySQL và khởi động lại backend."
