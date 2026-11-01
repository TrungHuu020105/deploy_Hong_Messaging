"""Local FastAPI entry point. Run: python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations

from contextlib import asynccontextmanager
import re
import secrets
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.domain import GRADE_LABELS, PRICE_TABLE, REASONS, TIERS
from backend.service import AppService
from hong_core import ROOT, today_vn
from hong_tiktok import SENDER_REVISION
from hong_database import load_environment


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SyncRequest(StrictModel):
    start_date: date
    end_date: date


class PreviewRequest(StrictModel):
    batch_size: int = Field(default=10,ge=1,le=1000)
    usernames: list[str] | None = None
    min_day: int = Field(default=1,ge=0,le=15)
    max_day: int = Field(default=15,ge=0,le=15)


class SendRequest(PreviewRequest):
    live: bool = False
    delay_seconds: int = Field(default=10,ge=0,le=3600)


class ScheduleRequest(StrictModel):
    enabled: bool
    interval_hours: float = Field(ge=0.25,le=168)
    batch_size: int = Field(ge=1,le=1000)
    repeat: bool = True
    live: bool = False
    delay_seconds: int = Field(default=10,ge=0,le=3600)
    min_day: int = Field(default=1,ge=0,le=15)
    max_day: int = Field(default=15,ge=0,le=15)


class OptOutRequest(StrictModel):
    opted_out: bool


class OrderKey(StrictModel):
    shop_id: str = Field(min_length=1,max_length=200)
    order_id: str = Field(min_length=1,max_length=200)


class CustomerProfileRequest(StrictModel):
    key: str = Field(min_length=1,max_length=500)
    tags: list[str] = Field(max_length=20)
    delete_orders: list[OrderKey] = Field(default_factory=list,max_length=1000)


class ReviewRequest(StrictModel):
    action: Literal["confirmed_sent","confirmed_not_sent","cancel"]
    note: str = Field(min_length=3,max_length=500)


def guarded(call,*args,**kwargs):
    try:
        return call(*args,**kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from None


def setting_flag(settings,key,default=False):
    value = str(settings.get(key,str(default))).strip().lower()
    if value in {"1","true","yes","on"}: return True
    if value in {"0","false","no","off",""}: return False
    raise ValueError(f"{key} phải là true hoặc false.")


def create_app(service: AppService | None = None, *, start_worker=True, mode=None,
               allowed_hosts=None, edit_token=None, allow_public_edits=None):
    settings = load_environment()
    app_mode = mode or (service.mode if service else settings.get("APP_MODE","admin"))
    if app_mode not in {"admin","web"}:
        raise ValueError("APP_MODE phải là admin hoặc web.")
    if service is not None and service.mode != app_mode:
        raise ValueError("Chế độ backend không khớp cấu hình ứng dụng.")
    service = service or AppService(auto_sync_enabled=setting_flag(settings,"AUTO_SYNC_ENABLED",True),
                                    initialize=False,mode=app_mode)
    token = settings.get("WEB_EDIT_TOKEN","") if edit_token is None else edit_token
    public_edits = setting_flag(settings,"WEB_ALLOW_PUBLIC_EDITS",app_mode == "web") if allow_public_edits is None else allow_public_edits
    local_paths = {"/api/send/preview","/api/send/run","/api/browser","/api/browser/open",
                   "/api/browser/confirm","/api/browser/close","/api/schedule","/api/messages/{message_id}/review"}
    hosts = allowed_hosts or [h.strip() for h in settings.get("ALLOWED_HOSTS","localhost,127.0.0.1,testserver").split(",") if h.strip()]

    @asynccontextmanager
    async def lifespan(_app):
        if not service._db_initialized:
            service.init_db()
        if start_worker:
            service.start()
        else:
            service.purge_expired()
        try:
            yield
        finally:
            if start_worker:
                service.stop()

    app = FastAPI(title="HONG — Chăm sóc khách Macca Ánh Dương",version="1.0",lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)
    app.state.service = service

    @app.middleware("http")
    async def web_permissions(request:Request,call_next):
        if app_mode == "web":
            path = request.url.path.rstrip("/")
            if (path.startswith("/api/send/") or path.startswith("/api/browser") or path == "/api/schedule"
                    or re.fullmatch(r"/api/messages/[^/]+/review",path)):
                return JSONResponse({"detail":"Chức năng này chỉ có trên máy local admin."},status_code=403)
            if request.method not in {"GET","HEAD","OPTIONS"} and path.startswith("/api/") and not public_edits:
                authorization = request.headers.get("Authorization","")
                supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
                if not token:
                    return JSONResponse({"detail":"VPS chưa cấu hình mã quản trị để sửa dữ liệu."},status_code=403)
                if not secrets.compare_digest(supplied.encode(),token.encode()):
                    return JSONResponse({"detail":"Nhập mã quản trị để sửa dữ liệu."},status_code=401)
        return await call_next(request)

    @app.post("/api/editor/check",include_in_schema=False)
    def check_editor():
        return {"ok":True}

    @app.get("/api/health")
    def health():
        return {"ok":True,"browser":service.browser_state(),"sender_revision":SENDER_REVISION,
                "database":"mysql" if service.db_path is None else "sqlite-test","mode":app_mode}

    @app.get("/api/config")
    def config():
        return {"rules":service.get_rules(),"schedule":service.get_schedule() if app_mode == "admin" else None,
                "price_table":PRICE_TABLE,"tiers":TIERS,"grade_labels":GRADE_LABELS,
                "reasons":REASONS,"today":today_vn().isoformat(),"sender_revision":SENDER_REVISION,
                "mode":app_mode,"capabilities":{"sending":app_mode == "admin",
                "editing":app_mode == "admin" or bool(token) or public_edits,
                "editing_auth_required":app_mode == "web" and not public_edits}}

    @app.get("/api/overview")
    def overview():
        result = service.overview()
        if app_mode == "web":
            result.pop("schedule",None)
            result.pop("browser",None)
        return result

    @app.get("/api/orders")
    def orders(include_excluded:bool=True):
        return {"items":service.orders(include_excluded=include_excluded)}

    @app.get("/api/customers")
    def customers(include_excluded:bool=False):
        return {"items":service.customers(include_excluded=include_excluded)}

    @app.put("/api/customer-profile")
    def save_customer_profile(request:CustomerProfileRequest):
        return guarded(service.save_customer_profile,request.key,request.tags,
                       [order.model_dump() for order in request.delete_orders])

    @app.post("/api/sync")
    def sync(request:SyncRequest):
        if request.start_date > request.end_date:
            raise HTTPException(status_code=400,detail="Ngày bắt đầu phải trước hoặc trùng ngày kết thúc.")
        try:
            return service.sync_orders(request.start_date.isoformat(),request.end_date.isoformat())
        except ValueError as exc:
            raise HTTPException(status_code=400,detail=str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(status_code=502,detail=str(exc)) from None

    @app.post("/api/purge")
    def purge():
        return {"deleted_orders":service.purge_expired()}

    @app.post("/api/send/preview")
    def preview(request:PreviewRequest):
        if request.min_day > request.max_day:
            raise HTTPException(status_code=400,detail="Khoảng ngày không hợp lệ.")
        return {"items":service.preview(request.batch_size,request.usernames,request.min_day,request.max_day)}

    @app.post("/api/send/run")
    def send(request:SendRequest):
        return guarded(service.create_job,live=request.live,batch_size=request.batch_size,
                       min_day=request.min_day,max_day=request.max_day,delay_seconds=request.delay_seconds,
                       usernames=request.usernames)

    @app.get("/api/jobs")
    def jobs(limit:int=Query(default=30,ge=1,le=200)):
        return {"items":service.jobs(limit)}

    @app.get("/api/messages")
    def messages(limit:int=Query(default=200,ge=1,le=1000)):
        return {"items":service.messages(limit)}

    @app.post("/api/messages/{message_id}/review")
    def review(message_id:int,request:ReviewRequest):
        return guarded(service.review,message_id,request.action,request.note)

    @app.post("/api/customers/{customer_key}/opt-out")
    def opt_out(customer_key:str,request:OptOutRequest):
        return guarded(service.set_opt_out,customer_key,request.opted_out)

    @app.get("/api/browser")
    def browser():
        return service.browser_state()

    @app.post("/api/browser/open")
    def browser_open():
        try:
            return service.open_browser()
        except Exception as exc:
            from selenium.common.exceptions import WebDriverException
            if isinstance(exc,WebDriverException) and "tab crashed" in str(exc).lower():
                detail = "Tab Chrome bị đóng khi mở TikTok. Hãy chạy backend trực tiếp trên máy với quyền truy cập Chrome."
            elif isinstance(exc,ValueError):
                detail = str(exc)
            else:
                detail = f"Không mở được Chrome ({type(exc).__name__}). Kiểm tra ChromeDriver và tệp cookie."
            raise HTTPException(status_code=400,detail=detail) from None

    @app.post("/api/browser/confirm")
    def browser_confirm():
        return guarded(service.confirm_browser)

    @app.post("/api/browser/close")
    def browser_close():
        return guarded(service.close_browser)

    @app.get("/api/schedule")
    def schedule():
        return service.get_schedule()

    @app.put("/api/schedule")
    def schedule_save(request:ScheduleRequest):
        return guarded(service.save_schedule,request.model_dump())

    if app_mode == "web":
        app.router.routes = [route for route in app.router.routes if getattr(route,"path",None) not in local_paths]

    dist = ROOT / "frontend" / "dist"

    @app.get("/{path:path}",include_in_schema=False)
    def frontend(path:str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404,detail="Không tìm thấy API.")
        target = (dist/path).resolve()
        if dist.exists() and target.is_relative_to(dist.resolve()) and target.is_file():
            return FileResponse(target)
        index = dist/"index.html"
        if index.exists():
            return FileResponse(index)
        return {"message":"Giao diện chưa được build. Chạy npm install và npm run build trong thư mục frontend."}

    return app


app = create_app()
