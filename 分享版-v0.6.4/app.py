"""Douyin Spark Keeper：多账号抖音续火花 Web 服务入口。

Windows 本机运行：python app.py（首次运行自动生成访问令牌写入 .env）
或直接双击 start.bat。
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import secrets
import sys
import threading
import urllib.request
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import accounts, automation, autostart, browsers, login, notify, scheduler
from core.config import (
    BASE_DIR,
    DATA_DIR,
    load_account_config,
    load_global_config,
    save_account_config,
    save_global_config,
)
from core.runtime import (
    close_account_log,
    load_runtime,
    read_account_log_tail,
    recent_logs,
    record_contacts,
    record_run,
    set_log_account,
    set_running,
    setup_account_log,
    setup_logging,
    update_runtime,
)

# 打包（PyInstaller）后 __file__ 指向内置目录；基准目录统一由 core.config 解析
# （开发=项目根目录；打包=exe 旁；单文件版=exe 旁 DouyinSparkData），此处不再各写一份
_BASE = BASE_DIR
# 网页资源打包进 _MEIPASS（onedir=_internal，onefile=临时解压目录），数据目录保持 exe 旁
STATIC_DIR = Path(getattr(sys, "_MEIPASS", _BASE)) / "static"
ENV_PATH = BASE_DIR / ".env"
# 打包后：浏览器下载/读取固定在 exe 旁 browsers 目录，绝不进系统缓存
if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BASE_DIR / "browsers")
elif (BASE_DIR / "browsers").exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BASE_DIR / "browsers")
APP_VERSION = "0.6.4"


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _ensure_token() -> str:
    """没有配置 AUTH_TOKEN 时自动生成一个随机令牌并写入 .env，避免裸奔。"""
    token = os.environ.get("AUTH_TOKEN", "").strip()
    if token:
        return token
    token = secrets.token_hex(16)
    try:
        with ENV_PATH.open("a", encoding="utf-8") as f:
            if ENV_PATH.exists() and ENV_PATH.stat().st_size > 0:
                f.write(f"\nAUTH_TOKEN={token}\n")
            else:
                f.write(f"# 网页访问令牌（自动生成，请勿外泄）\nAUTH_TOKEN={token}\nPORT=8000\n")
    except Exception:
        pass
    os.environ["AUTH_TOKEN"] = token
    return token


_load_env()
AUTH_TOKEN = _ensure_token()
logger = setup_logging()
# 令牌只打到控制台（print），不进日志/网页，避免泄露
print(f"访问令牌: {AUTH_TOKEN}  （保存在 {ENV_PATH}）")

run_lock = threading.Lock()
contacts_fetching: dict[str, bool] = {}
streak_syncing: dict[str, bool] = {}
image_diag_running: dict[str, bool] = {}


def _check_auth(token: str) -> None:
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="访问令牌不正确")


def _get_account(acc_id: str) -> Path:
    try:
        acc_dir = accounts.account_dir(acc_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法账号 ID")
    if not acc_dir.exists():
        raise HTTPException(status_code=404, detail="账号不存在")
    return acc_dir


def _notify_run(acc_id: str, acc_name: str, result: dict) -> None:
    try:
        title, content = notify.run_summary(acc_name, result)
        gcfg = load_global_config()
        res = notify.send_notification(gcfg, title, content)
        for r in res:
            logger.info("【%s】通知结果: %s", acc_id, r)
    except Exception as e:
        logger.warning("发送通知异常: %s", str(e)[:100])


def _start_run(
    acc_id: str,
    dry: bool,
    only_names: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行（多账号串行执行）")

    def worker() -> None:
        acc_dir = None
        try:
            acc_dir = _get_account(acc_id)
            setup_account_log(acc_id)
            set_log_account(acc_id)
            acc_cfg = load_account_config(acc_dir)
            set_running(acc_dir, True)
            try:
                result = automation.run_send(acc_dir, dry_run=dry, only_names=only_names)
                # 定时触发时把实际随机延迟秒数写入历史记录（手动/补发运行则无此字段）
                if extra and "delay_seconds" in extra:
                    result["delay_seconds"] = extra["delay_seconds"]
                record_run(acc_dir, result)
                logger.info(
                    "【%s】本次发送完成：成功 %s 人，失败 %s 人，dry=%s",
                    acc_id,
                    len(result.get("ok", [])),
                    len(result.get("failed", [])),
                    dry,
                )
                _notify_run(acc_id, acc_cfg.get("name", acc_id), result)

                if not dry:
                    failed_names = [
                        f["name"]
                        for f in result.get("failed", [])
                        if isinstance(f, dict)
                        and isinstance(f.get("name"), str)
                        and f["name"] != "_system"
                    ]
                    if failed_names and not result.get("logged_out"):
                        rt = load_runtime(acc_dir)
                        today = datetime.now().date().isoformat()
                        if rt.get("retry_date") != today:
                            update_runtime(acc_dir, retry_date=today)
                            scheduler.schedule_retry(
                                acc_id,
                                lambda: _start_run(acc_id, False, failed_names),
                                delay_minutes=int(acc_cfg.get("retry_minutes", 45)),
                            )
                    else:
                        scheduler.cancel_retry(acc_id)
            finally:
                set_running(acc_dir, False)
        except HTTPException as e:
            logger.warning("【%s】任务启动失败: %s", acc_id, e.detail)
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_fetch_contacts(acc_id: str) -> None:
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    contacts_fetching[acc_id] = True

    def worker() -> None:
        try:
            acc_dir = _get_account(acc_id)
            setup_account_log(acc_id)
            set_log_account(acc_id)
            try:
                record_contacts(acc_dir, automation.fetch_chat_contacts(acc_dir))
            finally:
                contacts_fetching[acc_id] = False
        except HTTPException as e:
            logger.warning("获取联系人失败: %s", e.detail)
            contacts_fetching[acc_id] = False
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_sync_streaks(acc_id: str) -> None:
    """后台完整抓取聊天列表，更新好友名单的火花天数/是否重燃。"""
    if not run_lock.acquire(blocking=False):
        if any(streak_syncing.values()):
            raise HTTPException(status_code=409, detail="已有账号正在同步火花中，请稍候")
        raise HTTPException(status_code=409, detail="已有其他任务在运行，请稍候")
    streak_syncing[acc_id] = datetime.now().isoformat(timespec="seconds")

    def worker() -> None:
        try:
            acc_dir = _get_account(acc_id)
            setup_account_log(acc_id)
            set_log_account(acc_id)
            try:
                res = automation.sync_friend_streaks(acc_dir)
                if res.get("error"):
                    logger.warning("【%s】同步火花失败: %s", acc_id, res["error"])
                else:
                    logger.info(
                        "【%s】同步火花完成：更新 %s 位，重燃 %s",
                        acc_id,
                        res.get("updated", 0),
                        "、".join(res.get("rekindled") or []) or "无",
                    )
                update_runtime(
                    acc_dir,
                    streaks_synced_at=res.get("at"),
                    streaks_sync_error=res.get("error"),
                )
            finally:
                streak_syncing[acc_id] = False
        except HTTPException as e:
            logger.warning("同步火花失败: %s", e.detail)
            streak_syncing[acc_id] = False
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_image_diag(acc_id: str, test_target: str = "") -> None:
    """真实图片发送测试（开发/排障用）。"""
    if not run_lock.acquire(blocking=False):
        if any(streak_syncing.values()):
            raise HTTPException(status_code=409, detail="已有账号正在同步火花中，请稍候")
        raise HTTPException(status_code=409, detail="已有其他任务在运行，请稍候")
    image_diag_running[acc_id] = True

    def worker() -> None:
        try:
            acc_dir = _get_account(acc_id)
            setup_account_log(acc_id)
            set_log_account(acc_id)
            try:
                if test_target:
                    res = automation.test_send_image(acc_dir, test_target)
                    if res.get("error"):
                        logger.warning("【%s】图片发送测试失败: %s", acc_id, res["error"])
                    else:
                        logger.info("【%s】图片发送测试完成，sent=%s", acc_id, res.get("sent"))
            finally:
                image_diag_running[acc_id] = False
        except HTTPException as e:
            logger.warning("图片诊断失败: %s", e.detail)
            image_diag_running[acc_id] = False
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_emoji_diag(acc_id: str) -> None:
    """侦查表情/贴纸面板入口（不发送任何消息）。"""
    if not run_lock.acquire(blocking=False):
        if any(streak_syncing.values()):
            raise HTTPException(status_code=409, detail="已有账号正在同步火花中，请稍候")
        raise HTTPException(status_code=409, detail="已有其他任务在运行，请稍候")
    image_diag_running[acc_id] = True

    def worker() -> None:
        try:
            acc_dir = _get_account(acc_id)
            setup_account_log(acc_id)
            set_log_account(acc_id)
            try:
                res = automation.diagnose_emoji_panel(acc_dir)
                if res.get("error"):
                    logger.warning("【%s】表情诊断失败: %s", acc_id, res["error"])
                elif res.get("panel_found"):
                    logger.info("【%s】表情诊断完成：找到面板入口", acc_id)
                else:
                    logger.info("【%s】表情诊断完成：未找到表情面板", acc_id)
            finally:
                image_diag_running[acc_id] = False
        except HTTPException as e:
            logger.warning("表情诊断失败: %s", e.detail)
            image_diag_running[acc_id] = False
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_emoji_probe(
    acc_id: str, target: str = "", x: int | None = None, y: int | None = None,
    step: int = 0, send: bool = False, text: str = "火花",
) -> None:
    """有头窗口探测表情 UI / 三步原生表情流；可传坐标或 step。"""
    if not run_lock.acquire(blocking=False):
        if any(streak_syncing.values()):
            raise HTTPException(status_code=409, detail="已有账号正在同步火花中，请稍候")
        raise HTTPException(status_code=409, detail="已有其他任务在运行，请稍候")
    image_diag_running[acc_id] = True

    def worker() -> None:
        try:
            acc_dir = _get_account(acc_id)
            setup_account_log(acc_id)
            set_log_account(acc_id)
            try:
                if step:
                    res = automation.emoji_flow_probe(acc_dir, target_name=target, max_step=int(step), send=bool(send), emoji_text=text or "火花")
                else:
                    res = automation.emoji_ui_probe(acc_dir, target_name=target, click_x=x, click_y=y)
                if res.get("error"):
                    logger.warning("【%s】表情UI探测失败: %s", acc_id, res["error"])
                else:
                    logger.info("【%s】表情UI探测完成 clicked=%s steps=%s", acc_id, res.get("clicked"), res.get("steps_done"))
            finally:
                image_diag_running[acc_id] = False
        except HTTPException as e:
            logger.warning("表情UI探测失败: %s", e.detail)
            image_diag_running[acc_id] = False
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_emoji_rec(acc_id: str, target: str = "") -> None:
    """开启表情操作录制窗口（有头，用户手动点，自动记录坐标与截图）。"""
    if not run_lock.acquire(blocking=False):
        if any(streak_syncing.values()):
            raise HTTPException(status_code=409, detail="已有账号正在同步火花中，请稍候")
        raise HTTPException(status_code=409, detail="已有其他任务在运行，请稍候")
    image_diag_running[acc_id] = True

    def worker() -> None:
        try:
            acc_dir = _get_account(acc_id)
            setup_account_log(acc_id)
            set_log_account(acc_id)
            try:
                res = automation.emoji_record_session(acc_id, acc_dir, target_name=target)
                if res.get("error"):
                    logger.warning("【%s】录制失败: %s", acc_id, res["error"])
                else:
                    logger.info("【%s】录制完成，事件 %s 条", acc_id, res.get("events"))
            finally:
                image_diag_running[acc_id] = False
        except HTTPException as e:
            logger.warning("录制失败: %s", e.detail)
            image_diag_running[acc_id] = False
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        accounts.ensure_accounts()
        scheduler.configure(lambda acc_id, extra=None: _start_run(acc_id, False, extra=extra))
    except Exception as e:  # pragma: no cover
        logger.warning("初始化失败: %s", e)
    yield
    scheduler.shutdown()


app = FastAPI(title="Douyin Spark Keeper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RunBody(BaseModel):
    dry: bool = False
    only_names: list[str] | None = None


class ReorderBody(BaseModel):
    ids: list[str]


class NameBody(BaseModel):
    name: str


class ConfigBody(BaseModel):
    config: dict


class TokenBody(BaseModel):
    token: str


class LoginStartBody(BaseModel):
    engine: str = "chromium"
    mode: str = "dialog"  # dialog=网页内二维码(无头) | window=弹出真实浏览器窗口


class AutoStartBody(BaseModel):
    enabled: bool = False
    minimized: bool = True  # True=最小化运行 False=正常窗口


@app.get("/")
def index() -> FileResponse:
    resp = FileResponse(STATIC_DIR / "index.html")
    # 禁止缓存，避免升级后浏览器仍显示旧页面
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# 抖音图片有防盗链：浏览器直接 <img> 会被 403，这里由后端带 Referer 代拉
_AVATAR_ALLOWED_HOSTS = (
    "douyinpic.com", "douyin.com", "douyinimg.com",
    "snssdk.com", "byteimg.com", "ixigua.com",
)
_AVATAR_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@app.get("/api/avatar")
def api_avatar(
    url: str = Query(...),
    token: str = Query(default=""),
) -> Response:
    _check_auth(token)
    try:
        u = urlparse(url)
        if u.scheme not in ("https", "http") or not u.hostname:
            raise HTTPException(status_code=400, detail="无效的图片地址")
        if not any(u.hostname.endswith(h) for h in _AVATAR_ALLOWED_HOSTS):
            raise HTTPException(status_code=400, detail="不允许的图片域名")
        req = urllib.request.Request(url, headers={
            "User-Agent": _AVATAR_UA,
            "Referer": "https://www.douyin.com/",
        })
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            data = resp.read(2 * 1024 * 1024)  # 上限 2MB
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        resp2 = Response(content=data, media_type=ctype)
        resp2.headers["Cache-Control"] = "public, max-age=3600"
        return resp2
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取头像失败: {str(e)[:80]}")


# ---------- 表情/图片库（data/emojis，发送时走抖音原生图片通道） ----------
_EMOJI_EXTS = {".png", ".jpg", ".jpeg", ".gif"}
_EMOJI_MAX_MB = 10


def _emojis_dir() -> Path:
    d = DATA_DIR / "emojis"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_emoji_name(name: str) -> str:
    # 只取文件名部分，杜绝目录穿越
    return Path(name or "").name


@app.get("/api/emojis")
def api_emojis(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    d = _emojis_dir()
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix.lower() in _EMOJI_EXTS:
            out.append({"name": p.name, "size": p.stat().st_size, "ext": p.suffix.lower()})
    return {"emojis": out}


@app.post("/api/emojis/upload")
async def api_emojis_upload(
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    fname = _safe_emoji_name(file.filename or "")
    if not fname:
        raise HTTPException(status_code=400, detail="缺少文件名")
    if Path(fname).suffix.lower() not in _EMOJI_EXTS:
        raise HTTPException(status_code=400, detail=f"仅支持 {'/'.join(sorted(_EMOJI_EXTS))} 格式")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(data) > _EMOJI_MAX_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"图片不能超过 {_EMOJI_MAX_MB}MB")
    d = _emojis_dir()
    dest = d / fname
    # 重名自动加 (1)(2)…
    stem, ext = os.path.splitext(fname)
    i = 1
    while dest.exists():
        dest = d / f"{stem}({i}){ext}"
        i += 1
    dest.write_bytes(data)
    return {"ok": True, "name": dest.name}


@app.post("/api/emojis/delete")
def api_emojis_delete(body: NameBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    d = _emojis_dir()
    p = d / _safe_emoji_name(body.name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        p.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    return {"ok": True}


@app.get("/api/emojis/file")
def api_emojis_file(name: str = Query(...), token: str = Query(default="")) -> Response:
    _check_auth(token)
    fname = _safe_emoji_name(name)
    if not fname or Path(fname).suffix.lower() not in _EMOJI_EXTS:
        raise HTTPException(status_code=400, detail="非法文件名")
    p = _emojis_dir() / fname
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    resp = Response(content=p.read_bytes(), media_type=ctype)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ---------- 官方原生贴纸图标（data/sticker_icons，仅供界面预览，非可上传图库） ----------

def _sticker_icons_dir() -> Path:
    d = DATA_DIR / "sticker_icons"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/sticker-icons")
def api_sticker_icons(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    d = _sticker_icons_dir()
    out = []
    for p in sorted(d.glob("*.png")):
        out.append({"name": p.stem, "file": p.name, "size": p.stat().st_size})
    return {"icons": out}


@app.get("/api/sticker-icons/file")
def api_sticker_icons_file(name: str = Query(...), token: str = Query(default="")) -> Response:
    _check_auth(token)
    fname = Path(name or "").name
    if not fname or Path(fname).suffix.lower() != ".png":
        raise HTTPException(status_code=400, detail="非法文件名")
    p = _sticker_icons_dir() / fname
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    resp = Response(content=p.read_bytes(), media_type="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ---------- 账号管理 ----------

@app.get("/api/accounts")
def api_accounts(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    accs = accounts.list_accounts()
    for a in accs:
        a["next_run"] = scheduler.next_run_time(a["id"])
    return {"accounts": accs, "version": APP_VERSION}


@app.post("/api/accounts/reorder")
def api_accounts_reorder(body: ReorderBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    accounts.save_order(body.ids)
    return {"ok": True, "order": body.ids}


@app.post("/api/accounts")
def api_account_create(body: NameBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    acc_id = accounts.create_account(body.name)
    scheduler.apply_schedule()
    return {"ok": True, "id": acc_id}


@app.put("/api/accounts/{acc_id}")
def api_account_rename(
    acc_id: str, body: NameBody, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    try:
        cfg = accounts.rename_account(acc_id, body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "config": cfg}


@app.delete("/api/accounts/{acc_id}")
def api_account_delete(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        close_account_log(acc_id)   # 先释放日志文件句柄，否则 Windows 下目录删不掉
        accounts.delete_account(acc_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True}


@app.post("/api/accounts/{acc_id}/upload-state")
async def api_upload_state(
    acc_id: str,
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    acc_dir = _get_account(acc_id)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    (acc_dir / "state.json").write_bytes(raw)
    logger.info("【%s】已更新登录态 state.json（%s 字节）", acc_id, len(raw))
    return {"ok": True, "size": len(raw)}


# ---------- 账号数据 ----------

@app.get("/api/accounts/{acc_id}/status")
def api_account_status(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    acc_dir = _get_account(acc_id)
    rt = load_runtime(acc_dir)
    cfg = load_account_config(acc_dir)
    return {
        "state_file_exists": (acc_dir / "state.json").exists(),
        "session_status": rt.get("session_status", "unknown"),
        "running": rt.get("running", False),
        "last_run": rt.get("last_run"),
        "history": rt.get("history", []),
        "next_run": scheduler.next_run_time(acc_id),
        "contacts": rt.get("contacts", []),
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": bool(contacts_fetching.get(acc_id, False)),
        "fetch_phase": automation.fetch_progress.get(acc_id, ""),
        "diag_image": bool(image_diag_running.get(acc_id, False)),
        "syncing_streaks": bool(streak_syncing.get(acc_id, False)),
        "streak_syncing_any": any(streak_syncing.values()),
        "streak_syncing_acc": next((k for k, v in streak_syncing.items() if v), None),
        "streak_syncing_since": next((v for v in streak_syncing.values() if v), None),
        "streaks_synced_at": rt.get("streaks_synced_at"),
        "streaks_sync_error": rt.get("streaks_sync_error"),
        "config": cfg,
    }


@app.get("/api/accounts/{acc_id}/config")
def api_account_config(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    acc_dir = _get_account(acc_id)
    return load_account_config(acc_dir)


@app.put("/api/accounts/{acc_id}/config")
def api_account_config_save(
    acc_id: str, body: ConfigBody, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    acc_dir = _get_account(acc_id)
    try:
        cfg = save_account_config(acc_dir, body.config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "config": cfg}


@app.post("/api/accounts/{acc_id}/contacts/fetch")
def api_contacts_fetch(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _start_fetch_contacts(acc_id)
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/accounts/{acc_id}/streaks/sync")
def api_streaks_sync(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _start_sync_streaks(acc_id)
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/accounts/{acc_id}/emoji-probe")
def api_emoji_probe(acc_id: str, body: dict | None = None, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    b = body or {}
    try:
        _start_emoji_probe(
            acc_id,
            target=str(b.get("target", "")),
            x=b.get("x"),
            y=b.get("y"),
            step=int(b.get("step") or 0),
            send=bool(b.get("send")),
            text=str(b.get("text") or "火花"),
        )
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/accounts/{acc_id}/emoji-record")
def api_emoji_record(acc_id: str, body: dict | None = None, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    b = body or {}
    try:
        _start_emoji_rec(acc_id, target=str(b.get("target", "")))
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/accounts/{acc_id}/emoji-record-stop")
def api_emoji_record_stop(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _get_account(acc_id)
    except HTTPException:
        raise
    automation.request_emoji_rec_stop(acc_id)
    return {"ok": True, "stopping": True}


@app.post("/api/accounts/{acc_id}/emoji-diag")
def api_emoji_diag(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _start_emoji_diag(acc_id)
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/accounts/{acc_id}/image-test")
def api_image_test(acc_id: str, body: dict | None = None, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    target = (body or {}).get("target", "")
    if not target:
        raise HTTPException(status_code=400, detail="缺少 target（目标好友名）")
    try:
        _start_image_diag(acc_id, test_target=target)
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/accounts/{acc_id}/run")
def api_run(acc_id: str, body: RunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _start_run(acc_id, bool(body.dry), only_names=body.only_names)
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/run-all")
def api_run_all(body: RunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """一键让所有启用的账号各跑一轮（串行，逐个执行）。"""
    _check_auth(token)
    enabled = [a for a in accounts.list_accounts() if a.get("enabled")]
    if not enabled:
        raise HTTPException(status_code=400, detail="没有启用的账号")
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行（多账号串行执行）")
    dry = bool(body.dry)

    def worker() -> None:
        try:
            for a in enabled:
                acc_id = a["id"]
                try:
                    acc_dir = _get_account(acc_id)
                except HTTPException:
                    continue
                if not (acc_dir / "state.json").exists():
                    logger.warning("【%s】跳过：无登录态", acc_id)
                    continue
                setup_account_log(acc_id)
                set_log_account(acc_id)
                acc_cfg = load_account_config(acc_dir)
                set_running(acc_dir, True)
                try:
                    result = automation.run_send(acc_dir, dry_run=dry)
                    record_run(acc_dir, result)
                    logger.info(
                        "【%s】批量发送完成：成功 %s 人，失败 %s 人，dry=%s",
                        acc_id,
                        len(result.get("ok", [])),
                        len(result.get("failed", [])),
                        dry,
                    )
                    _notify_run(acc_id, acc_cfg.get("name", acc_id), result)
                finally:
                    set_running(acc_dir, False)
        finally:
            set_log_account(None)
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "started": True, "accounts": len(enabled)}


@app.get("/api/backup")
def api_backup(token: str = Header(default="", alias="X-Auth-Token")) -> StreamingResponse:
    """一键备份：把 data/ 下全部账号数据打包成 zip 下载。"""
    _check_auth(token)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        files = 0
        for p in DATA_DIR.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            zf.write(p, p.relative_to(DATA_DIR))
            files += 1
        if files == 0:
            zf.writestr("说明.txt", "暂无账号数据。")
    buf.seek(0)
    fname = f"douyin-spark-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/backup/restore")
async def api_backup_restore(
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    """一键恢复：上传备份 zip，解包回 data/ 目录（防路径穿越，不覆盖运行中的任务）。"""
    _check_auth(token)
    if run_lock.locked():
        raise HTTPException(status_code=409, detail="有任务正在运行，请稍后再恢复")
    raw = await file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（最大 50MB）")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 备份文件")
    data_root = DATA_DIR.resolve()
    count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                target = (data_root / name).resolve()
                if target != data_root and data_root not in target.parents:
                    raise HTTPException(status_code=400, detail=f"备份包包含非法路径: {name}")
                if "__pycache__" in target.parts:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                count += 1
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="不是合法的 zip 备份包")
    scheduler.apply_schedule()
    logger.info("备份恢复完成：%s 个文件", count)
    return {"ok": True, "files": count}


@app.get("/api/accounts/{acc_id}/logs")
def api_account_logs(
    acc_id: str, n: int = 300, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    acc_dir = _get_account(acc_id)
    return {"logs": read_account_log_tail(acc_dir, max(10, min(n, 600)))}


# ---------- 浏览器检测与网页内扫码登录 ----------

@app.get("/api/browsers")
def api_browsers(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    import sys as _sys
    return {
        "browsers": browsers.detect_windows_browsers(),
        "engines": browsers.detect_playwright_engines(),
        "platform": _sys.platform,
    }


@app.post("/api/accounts/{acc_id}/login/start")
async def api_login_start(
    acc_id: str,
    body: LoginStartBody,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, message = await login.start_login(acc_id, engine=body.engine, mode=body.mode)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True}


@app.get("/api/accounts/{acc_id}/login/qr")
async def api_login_qr(
    acc_id: str,
    full: int = 0,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    if full:
        qr, note = await login.full_screenshot(acc_id)
    else:
        qr, note = await login.qr_image(acc_id)
    if not qr:
        raise HTTPException(status_code=404, detail=f"画面暂不可用（{note}），请刷新或重新开始")
    text = await login.visible_text(acc_id)
    return {"qr": qr, "note": note, "text": text}


@app.post("/api/accounts/{acc_id}/login/status")
async def api_login_status(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    acc_dir = _get_account(acc_id)
    result = await login.poll_login(acc_id, acc_dir)
    if result.get("logged_in"):
        threading.Thread(
            target=login.maybe_rename_after_login, args=(acc_id, acc_dir), daemon=True
        ).start()
    return result


@app.post("/api/accounts/{acc_id}/login/cancel")
async def api_login_cancel(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    await login.cancel_login(acc_id)
    return {"ok": True}


class VerifyBody(BaseModel):
    code: str


class ManualClickBody(BaseModel):
    x: float
    y: float


class ManualTypeBody(BaseModel):
    text: str


class ManualDragBody(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class ManualScrollBody(BaseModel):
    dy: float


@app.post("/api/accounts/{acc_id}/login/verify")
async def api_login_verify(
    acc_id: str, body: VerifyBody, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, msg = await login.submit_verify_code(acc_id, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/accounts/{acc_id}/login/resend-code")
async def api_login_resend(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, msg = await login.resend_verify_code(acc_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/accounts/{acc_id}/login/manual-click")
async def api_login_manual_click(
    acc_id: str, body: ManualClickBody, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, msg = await login.manual_click(acc_id, body.x, body.y)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/accounts/{acc_id}/login/manual-type")
async def api_login_manual_type(
    acc_id: str, body: ManualTypeBody, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, msg = await login.manual_type(acc_id, body.text)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/accounts/{acc_id}/login/manual-enter")
async def api_login_manual_enter(acc_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, msg = await login.manual_enter(acc_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/accounts/{acc_id}/login/manual-drag")
async def api_login_manual_drag(
    acc_id: str, body: ManualDragBody, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, msg = await login.manual_drag(acc_id, body.x1, body.y1, body.x2, body.y2)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/accounts/{acc_id}/login/manual-scroll")
async def api_login_manual_scroll(
    acc_id: str, body: ManualScrollBody, token: str = Header(default="", alias="X-Auth-Token")
) -> dict:
    _check_auth(token)
    _get_account(acc_id)
    ok, msg = await login.manual_scroll(acc_id, body.dy)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


# ---------- 全局 ----------

@app.get("/api/logs")
def api_logs(n: int = 300, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


@app.get("/api/global/config")
def api_global_config(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return load_global_config()


@app.put("/api/global/config")
def api_global_config_save(body: ConfigBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return {"ok": True, "config": save_global_config(body.config)}


@app.post("/api/notify/test")
def api_notify_test(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    gcfg = load_global_config()
    results = notify.send_notification(gcfg, "抖音续火花：测试通知", "这是一条测试消息，收到即代表通知渠道配置正确。")
    return {"ok": True, "results": results, "warnings": notify.check_notification_health()}


# ---------- 开机自启动 ----------

@app.get("/api/autostart")
def api_autostart(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return {
        "enabled": autostart.is_enabled(),
        "minimized": autostart.get_mode(),
    }


@app.put("/api/autostart")
def api_autostart_save(body: AutoStartBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    if not autostart.set_enabled(bool(body.enabled), minimized=bool(body.minimized)):
        raise HTTPException(status_code=400, detail="开机自启动设置失败（可能权限不足），请手动检查「启动」文件夹")
    return {"ok": True, "enabled": autostart.is_enabled(), "minimized": autostart.get_mode()}


@app.put("/api/token")
def api_token_change(
    body: TokenBody,
    request: Request,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    global AUTH_TOKEN
    client_host = (request.client.host if request.client else "") or ""
    is_local = client_host in ("127.0.0.1", "::1", "localhost")
    # 本机回环允许无鉴权设置令牌（首次引导，方便直接自定义）；
    # 服务器 / 局域网来源必须带旧令牌，防止他人篡改
    if token:
        _check_auth(token)
    elif not is_local:
        raise HTTPException(status_code=401, detail="访问令牌不正确")
    new_token = body.token.strip()
    if len(new_token) < 8:
        raise HTTPException(status_code=400, detail="令牌至少 8 位")
    AUTH_TOKEN = new_token
    os.environ["AUTH_TOKEN"] = new_token
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("AUTH_TOKEN="):
            lines[i] = f"AUTH_TOKEN={new_token}"
            replaced = True
    if not replaced:
        lines.append(f"AUTH_TOKEN={new_token}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("访问令牌已更新")
    return {"ok": True, "token": new_token}


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"服务启动：http://{host}:{port}")
    if getattr(sys, "frozen", False) and host in ("127.0.0.1", "localhost"):
        # 打包版（本机运行）自动打开浏览器；服务器（HOST=0.0.0.0）不自动开
        import threading
        import time
        import webbrowser

        def _open_browser() -> None:
            time.sleep(2.5)  # 等服务起来
            try:
                webbrowser.open(f"http://{host}:{port}")
            except Exception:
                pass

        threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
