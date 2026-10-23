"""每天定时触发各账号的发送任务（每个启用的账号一条 cron 任务）。"""

from __future__ import annotations

import logging
from contextlib import contextmanager
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .accounts import account_dir, list_accounts
from . import ctx, workspace
from .config import load_account_config

logger = logging.getLogger("douyin-spark")
TZ = "Asia/Shanghai"

_scheduler: BackgroundScheduler | None = None
# run_func(acc_id, extra)：extra 携带本次触发的附加信息（如实际随机延迟秒数）
_run_func: Callable[[str, dict | None], None] | None = None


@contextmanager
def _in_ws(ws: str):
    """在这段代码执行期间，把数据路径切到指定工作区。

    定时任务是后台线程跑的，必须自己设定上下文，否则会读写到默认工作区的数据。
    """
    token = ctx.set_root(workspace.ws_paths(ws)["root"])
    try:
        yield
    finally:
        ctx.reset_root(token)


def _daily_job(ws: str, acc_id: str) -> None:
    with _in_ws(ws):
        _daily_job_inner(ws, acc_id)


def _daily_job_inner(ws: str, acc_id: str) -> None:
    cfg = load_account_config(account_dir(acc_id))
    # 注意：不能用 `or 30`，否则 jitter=0 会被当成 30
    jitter = max(0, int(cfg.get("jitter_minutes", 30)))
    delay = 0
    if jitter:
        delay = random.uniform(0, jitter * 60)
        logger.info("【%s】随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", acc_id, delay, jitter)
        time.sleep(delay)
    if _run_func:
        # 把本次实际延迟秒数传给运行层，写入历史记录（抖动=0 时为 0）
        _run_func(acc_id, {"delay_seconds": round(delay), "queued": True})


def configure(run_func: Callable[[str, dict | None], None]) -> None:
    global _scheduler, _run_func
    _run_func = run_func
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def apply_schedule() -> None:
    """重建所有账号的每日定时任务（启用的账号才安排）。"""
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith("daily_"):
            _scheduler.remove_job(job.id)

    # 遍历所有工作区：每个工作区的账号都在自己那份数据里安排任务
    for ws in workspace.list_workspaces():
        ws_id = ws["ws"]
        with _in_ws(ws_id):
            for acc in list_accounts():
                if not acc["enabled"]:
                    continue
                cfg = load_account_config(account_dir(acc["id"]))
                hh, mm = cfg.get("schedule_time", "21:00").split(":")
                _scheduler.add_job(
                    _daily_job,
                    CronTrigger(hour=int(hh), minute=int(mm), timezone=TZ),
                    id=f"daily_{ws_id}_{acc['id']}",
                    args=[ws_id, acc["id"]],
                    replace_existing=True,
                    coalesce=True,
                    misfire_grace_time=3600,
                )
                tag = "" if ws_id == workspace.DEFAULT_WS else "（工作区 %s）" % ws_id
                logger.info("【%s%s】定时任务已更新：每天 %s:%s (%s)", acc["name"], tag, hh, mm, TZ)


def next_run_time(acc_id: str) -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(f"daily_{workspace.current_ws()}_{acc_id}")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def schedule_retry(acc_id: str, run_func: Callable, delay_minutes: int = 45) -> None:
    if _scheduler is None:
        return
    ws = workspace.current_ws()
    job_id = f"retry_{ws}_{acc_id}"
    if _scheduler.get_job(job_id):
        return
    run_at = datetime.now() + timedelta(minutes=max(5, delay_minutes))

    def _wrapped() -> None:
        with _in_ws(ws):
            run_func()

    _scheduler.add_job(
        _wrapped,
        DateTrigger(run_date=run_at, timezone=TZ),
        id=job_id,
        replace_existing=True,
    )
    logger.info("【%s】已安排 %s 分钟后自动补发本次失败的好友", acc_id, delay_minutes)


def cancel_retry(acc_id: str) -> None:
    job_id = f"retry_{workspace.current_ws()}_{acc_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info("【%s】已取消待执行的补发任务", acc_id)


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
