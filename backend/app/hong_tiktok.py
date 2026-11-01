"""Visible Chrome session and conservative queue runner (never runs on import)."""
from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, unquote

from hong_core import claim_message, finish_message, pending_messages, requeue_missing_account


class AccountNotFoundError(RuntimeError):
    """TikTok explicitly reports that the requested profile does not exist."""


SENDER_REVISION = "missing-account-v2"
MISSING_ACCOUNT_TEXTS = (
    "couldn't find this account", "không tìm thấy tài khoản này",
    "không thể tìm thấy tài khoản này",
)
MISSING_ACCOUNT_LOCATOR = (
    "xpath", "//*[not(self::script) and not(self::style) and not(self::title) and "
    "(contains(.,'find this account') or contains(.,'tìm thấy tài khoản'))]",
)


def normalized_page_text(value):
    text = unicodedata.normalize("NFKC",value or "").casefold()
    text = text.translate(str.maketrans({"’":"'","‘":"'","ʼ":"'", "\u200b":None,"\ufeff":None}))
    return re.sub(r"\s*'\s*","'"," ".join(text.split()))


class TikTokSendTimeout(RuntimeError):
    """A timed-out stage with enough context to diagnose a stopped batch."""


def load_tiktok_cookies(path, now=None):
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy {path.name}. Hãy sửa COOKIE_FILE.")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("Tệp cookie không có định dạng JSON UTF-8 hợp lệ.") from None
    rows = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("Dữ liệu JSON phải là danh sách hoặc đối tượng có khóa cookies.")
    now = time.time() if now is None else now
    result, skipped = [], Counter()
    sites = {"strict":"Strict", "lax":"Lax", "none":"None", "no_restriction":"None"}
    for row in rows:
        if not isinstance(row, dict):
            skipped["sai định dạng"] += 1
            continue
        name, value = row.get("name"), row.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            skipped["sai name/value"] += 1
            continue
        domain = row.get("domain", "")
        if not isinstance(domain, str):
            skipped["sai domain"] += 1
            continue
        domain = domain.lower().strip()
        host = domain.lstrip(".")
        if host not in {"tiktok.com", "www.tiktok.com"}:
            skipped["tên miền không phù hợp"] += 1
            continue
        if row.get("hostOnly") is True:
            domain = host
            if host != "www.tiktok.com":
                skipped["cookie hostOnly không thuộc www.tiktok.com"] += 1
                continue
        path_value = row.get("path", "/")
        if not isinstance(path_value, str) or not path_value.startswith("/"):
            skipped["sai path"] += 1
            continue
        cookie = {"name":name,"value":value,"domain":domain,"path":path_value,
                  "secure":row.get("secure", False) is True,
                  "httpOnly":row.get("httpOnly", False) is True}
        expiry = row.get("expirationDate",row.get("expiry",row.get("expires")))
        if row.get("session") is not True and expiry is not None:
            try:
                if isinstance(expiry, bool):
                    raise ValueError
                expiry = float(expiry)
                if not math.isfinite(expiry):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                skipped["sai hạn sử dụng"] += 1
                continue
            if expiry != -1:
                if int(expiry) <= now:
                    skipped["hết hạn"] += 1
                    continue
                cookie["expiry"] = int(expiry)
        site = sites.get(str(row.get("sameSite", "")).lower())
        if site:
            cookie["sameSite"] = site
            if site == "None":
                cookie["secure"] = True
        result.append(cookie)
    return result, skipped


def open_browser(cookie_file, *, driver_path=None, page_timeout=45):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import NoSuchDriverException, SessionNotCreatedException, WebDriverException

    cookies, skipped = load_tiktok_cookies(cookie_file)
    if not cookies:
        raise ValueError("Không còn cookie phù hợp. Xuất lại cookie TikTok.")
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    options.add_argument("--start-maximized")
    profile = Path(__file__).resolve().parent / ".runtime" / "tiktok-chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--no-first-run")
    options.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications":2})
    kwargs = {"options":options}
    configured_driver = driver_path or os.environ.get("CHROMEDRIVER_PATH")
    if configured_driver:
        kwargs["service"] = Service(str(Path(configured_driver).resolve()))
    try:
        driver = webdriver.Chrome(**kwargs)
    except (NoSuchDriverException, SessionNotCreatedException):
        if configured_driver:
            raise
        # Selenium Manager may be unable to download in a restricted environment.
        local = sorted((Path(__file__).resolve().parent / ".runtime").glob("chromedriver-*/chromedriver-win64/chromedriver.exe"),reverse=True)
        if not local:
            raise
        driver = webdriver.Chrome(service=Service(str(local[0])),options=options)
    try:
        driver.set_page_load_timeout(page_timeout)
        driver.get("https://www.tiktok.com/robots.txt")
        WebDriverWait(driver,20).until(lambda d: d.execute_script("return document.readyState") in {"interactive","complete"})
        if urlsplit(driver.current_url).hostname != "www.tiktok.com":
            raise RuntimeError("Tên miền không đúng trước khi nạp cookie.")
        driver.delete_all_cookies()
        added = 0
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
                added += 1
            except WebDriverException:
                skipped["trình duyệt từ chối"] += 1
        if added == 0:
            raise RuntimeError("Chrome không nhận cookie nào.")
        driver.get("https://www.tiktok.com/")
        return driver
    except BaseException:
        driver.quit()
        raise


class TikTokSender:
    def __init__(self, driver, *, account_checked=False, timeout=20):
        if not account_checked:
            raise ValueError("Kiểm tra tài khoản gửi trên Chrome rồi đặt account_checked=True.")
        self.driver, self.timeout = driver, timeout
        self.enter_attempted = False
        self.stage = None

    def _on_profile(self, username):
        location = urlsplit(self.driver.current_url)
        actual = unquote(location.path).strip("/").casefold()
        return (location.scheme == "https" and location.hostname in {"tiktok.com","www.tiktok.com"}
                and actual == f"@{username.casefold()}")

    def _raise_if_missing_account(self, username):
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.common.by import By

        # Never classify an unrelated page, login redirect, or sent message as missing.
        if self.enter_attempted or not self._on_profile(username):
            return
        for element in self.driver.find_elements(*MISSING_ACCOUNT_LOCATOR):
            try:
                if element.is_displayed() and normalized_page_text(element.text).rstrip(".!。") in MISSING_ACCOUNT_TEXTS:
                    raise AccountNotFoundError("Không tìm thấy tài khoản TikTok; bỏ qua và giữ tin chưa gửi.")
            except StaleElementReferenceException:
                continue
        # TikTok can split a heading across spans, change punctuation, or add subtitles.
        # A matching title AND visible body confirm the page; title alone may be stale.
        title = re.sub(r"^(?:\(\d+\)\s*)+","",normalized_page_text(self.driver.title))
        if any(title.startswith(text) for text in MISSING_ACCOUNT_TEXTS):
            try:
                body = normalized_page_text(self.driver.find_element(By.TAG_NAME,"body").text)
            except StaleElementReferenceException:
                return
            if any(text in body for text in MISSING_ACCOUNT_TEXTS):
                raise AccountNotFoundError("Không tìm thấy tài khoản TikTok; bỏ qua và giữ tin chưa gửi.")

    def send(self, username, body):
        from selenium.common.exceptions import TimeoutException, WebDriverException

        self.enter_attempted = False
        self.stage = "tải trang hồ sơ"
        try:
            return self._send(username,body)
        except TimeoutException as error:
            # get() itself can time out even though TikTok has rendered its error page.
            try:
                self._raise_if_missing_account(username)
            except WebDriverException:
                pass  # Preserve the original timeout if the browser cannot be inspected.
            raise TikTokSendTimeout(f"Hết thời gian chờ tại bước {self.stage}.") from error

    def _send(self, username, body):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.common.exceptions import ElementClickInterceptedException, StaleElementReferenceException, TimeoutException
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver,self.timeout)
        driver.get(f"https://www.tiktok.com/@{username}")
        button_locator = (By.CSS_SELECTOR,'button[data-e2e="message-button"]')

        def check_profile(current_driver):
            if not self._on_profile(username):
                raise RuntimeError("Trang hồ sơ đã chuyển hướng hoặc không đúng người nhận.")
            self._raise_if_missing_account(username)

        def profile_ready(current_driver):
            check_profile(current_driver)
            return EC.element_to_be_clickable(button_locator)(current_driver)

        self.stage = "chờ nút nhắn tin trên hồ sơ"
        wait.until(profile_ready)
        def click_when_clear(locator, description):
            def attempt(current_driver):
                try:
                    if locator == button_locator:
                        check_profile(current_driver)
                    target = current_driver.find_element(*locator)
                    if not target.is_displayed() or not target.is_enabled():
                        return False
                    current_driver.execute_script("arguments[0].scrollIntoView({block:'center'})",target)
                    clear = current_driver.execute_script("""const e=arguments[0],r=e.getBoundingClientRect();
                        const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                        return top===e || e.contains(top);""",target)
                    if not clear:
                        return False
                    target.click()
                    return target
                except (ElementClickInterceptedException,StaleElementReferenceException):
                    return False
            try:
                return wait.until(attempt)
            except TimeoutException:
                self._raise_if_missing_account(username)
                raise RuntimeError(f"{description} bị cửa sổ khác che. Đóng thông báo trên TikTok rồi thử lại.") from None

        self.stage = "mở hội thoại"
        click_when_clear(button_locator,"Nút nhắn tin")
        textbox_locator = (By.CSS_SELECTOR,'div[contenteditable="true"][role="textbox"]')
        self.stage = "chờ ô nhập tin nhắn"
        textbox = wait.until(EC.element_to_be_clickable(textbox_locator))
        # Preserve the user's existing draft; don't send it or overwrite it.
        if (textbox.get_attribute("textContent") or "").strip():
            raise RuntimeError("Hội thoại có bản nháp sẵn. Kiểm tra trực tiếp trước khi tiếp tục.")
        self.stage = "chọn ô nhập tin nhắn"
        textbox = click_when_clear(textbox_locator,"Ô nhập tin nhắn")
        if (textbox.get_attribute("textContent") or "").strip():
            raise RuntimeError("Hội thoại có bản nháp sẵn. Kiểm tra trực tiếp trước khi tiếp tục.")
        # Chrome CDP supports emoji outside BMP, unlike some send_keys versions.
        self.stage = "nhập nội dung tin nhắn"
        driver.execute_cdp_cmd("Input.insertText", {"text":body})
        wait.until(lambda _: (textbox.get_attribute("textContent") or "").replace("\r\n","\n") == body.replace("\r\n","\n"))
        self.enter_attempted = True  # set BEFORE Enter: transport failure can still deliver
        self.stage = "nhấn gửi và chờ ô nhập trống"
        textbox.send_keys(Keys.ENTER)
        # Clear composer is only a UI acknowledgement, never proof of delivery.
        wait.until(lambda _: not (textbox.get_attribute("textContent") or "").strip())
        return "submitted"


def run_queue(db, *, campaign="aftercare_v1", sender=None, dry_run=True,
              approved_ids=(), batch_limit=None, daily_limit=None, cooldown_days=0,
              min_days=None, max_days=None, statuses=(3,), source="Tiktok", pause_seconds=10,
              enforce_current_batch=True, eligibility_check=None, stop_event=None):
    if (batch_limit is not None and batch_limit < 1) or pause_seconds < 0:
        raise ValueError("Số tin tối đa mỗi đợt hoặc thời gian nghỉ không hợp lệ.")
    messages = pending_messages(db,campaign)
    if dry_run:
        return messages[:batch_limit]
    if sender is None:
        raise ValueError("Chưa thiết lập bộ gửi tin (sender) cho chế độ gửi thật.")
    ids = set(approved_ids)
    if not ids:
        raise ValueError("Hãy chọn mã tin đã duyệt từ bản xem trước và điền vào approved_ids trước khi gửi thật.")
    results, attempted = [], 0
    for row in messages:
        if stop_event is not None and stop_event.is_set():
            break
        if row["id"] not in ids:
            continue
        if batch_limit is not None and attempted >= batch_limit:
            break
        if attempted and pause_seconds:
            if stop_event is not None:
                if stop_event.wait(pause_seconds):
                    break
            else:
                time.sleep(pause_seconds)
        message, reason = claim_message(db,row["id"],daily_limit=daily_limit,
                                       cooldown_days=cooldown_days,min_days=min_days,max_days=max_days,
                                       statuses=statuses,source=source,
                                       enforce_current_batch=enforce_current_batch,
                                       eligibility_check=eligibility_check)
        if not message:
            results.append({"id":row["id"],"state":"skipped","reason":reason})
            if reason == "daily_limit":
                break
            continue
        attempted += 1
        try:
            state = sender.send(message["username"],message["body"])
            if state != "submitted":
                raise RuntimeError("Bộ gửi tin không trả về trạng thái xác nhận hợp lệ.")
        except BaseException as error:
            if isinstance(error,AccountNotFoundError) and getattr(sender,"enter_attempted",True) is False:
                requeue_missing_account(db,message["id"])
                results.append({"id":message["id"],"state":"skipped","reason":"account_not_found",
                                "detail":str(error)})
                continue
            # Unknown adapters are ambiguous by default; never auto-retry them.
            state = "uncertain" if getattr(sender,"enter_attempted",True) else "failed"
            finish_message(db,message["id"],state,type(error).__name__)
            results.append({"id":message["id"],"state":state,"error":type(error).__name__,
                            "detail":str(error) if isinstance(error,RuntimeError) else None,
                            "stage":getattr(sender,"stage",None)})
            if isinstance(error,(KeyboardInterrupt,SystemExit)):
                raise
            # Stop batch on first error for login challenges / DOM changes.
            break
        else:
            finish_message(db,message["id"],state)
            results.append({"id":message["id"],"state":state})
    return results
