"""多令牌端到端隔离测试（B 组）。

真起一个服务实例（隔离在临时目录），用两个令牌分别操作，验证：
  * 老令牌照用（老部署行为不变）
  * 不同令牌 → 不同工作区，账号/通知配置互相看不见
  * 权限：普通令牌不能管理别人的工作区、不能删默认工作区
  * 轮换：只有自己的工作区能轮换，换完旧的立刻失效

跑法（在 多令牌版 目录下）：
    python tests/test_isolation_e2e.py
"""
from __future__ import annotations

import io
import json
import zipfile
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

SRC = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + extra) if extra else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(base: str, method: str, path: str, token: str = "", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    if token:
        r.add_header("X-Auth-Token", token)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)



def req_bytes(base: str, path: str, token: str = ""):
    """取二进制响应（备份 zip 用）。"""
    r = urllib.request.Request(base + path, method="GET")
    if token:
        r.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ise2e_"))
    proc = None
    try:
        for item in ("app.py",):
            shutil.copy(SRC / item, tmp / item)
        for d in ("core", "static"):
            shutil.copytree(SRC / d, tmp / d, ignore=shutil.ignore_patterns("__pycache__"))
        (tmp / "data").mkdir(exist_ok=True)
        shutil.copytree(SRC / "data" / "sticker_icons", tmp / "data" / "sticker_icons")

        legacy = "legacy" + "0" * 26
        port = free_port()
        (tmp / ".env").write_text(
            "HOST=127.0.0.1\nPORT=%d\nAUTH_TOKEN=%s\n" % (port, legacy), encoding="utf-8")

        proc = subprocess.Popen([sys.executable, "app.py"], cwd=str(tmp),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % port
        ok = False
        for _ in range(30):
            time.sleep(0.5)
            st, _b = req(base, "GET", "/api/health")
            if st == 200:
                ok = True
                break
        check("服务能启动并响应 /api/health", ok)
        if not ok:
            return 1

        print("\n[C1] 老部署行为：还没有 tokens.json 时，老令牌照用")
        st, me = req(base, "GET", "/api/me", legacy)
        check("老令牌 → default 工作区", st == 200 and me.get("ws") == "default", str(me))
        check("标识为自己是默认工作区", me.get("is_default") is True)
        st, _ = req(base, "GET", "/api/accounts")            # 不带令牌
        check("不带令牌 → 401", st == 401, str(st))

        print("\n[C2] 新建工作区，拿到一次性令牌")
        st, created = req(base, "POST", "/api/workspaces", legacy, {"name": "朋友1"})
        check("新建成功", st == 200 and created.get("token"), str(created)[:120])
        ws_b, tok_b = created["ws"], created["token"]
        st, me_b = req(base, "GET", "/api/me", tok_b)
        check("新令牌 → 新工作区", st == 200 and me_b.get("ws") == ws_b, str(me_b))
        check("新工作区不是默认区", me_b.get("is_default") is False)

        print("\n[C3] 账号隔离（核心）")
        st, a1 = req(base, "POST", "/api/accounts", legacy, {"name": "我的账号"})
        check("主令牌建账号成功", st == 200, str(a1))
        st, a2 = req(base, "POST", "/api/accounts", tok_b, {"name": "朋友的账号"})
        check("朋友令牌建账号成功", st == 200, str(a2))
        st, la = req(base, "GET", "/api/accounts", legacy)
        st2, lb = req(base, "GET", "/api/accounts", tok_b)
        names_a = [x["name"] for x in la.get("accounts", [])]
        names_b = [x["name"] for x in lb.get("accounts", [])]
        check("主令牌看到自己的账号、看不到朋友的（默认账号是程序自动建的，属正常）", "我的账号" in names_a and "朋友的账号" not in names_a, str(names_a))
        check("朋友令牌只看到自己的账号", names_b == ["朋友的账号"], str(names_b))

        print("\n[C3b] 调度器覆盖所有工作区（P3）：两个工作区的账号都要有定时任务")
        st, la2 = req(base, "GET", "/api/accounts", legacy)
        st2, lb2 = req(base, "GET", "/api/accounts", tok_b)
        def _nr(payload):
            return {it.get("name"): it.get("next_run") for it in payload.get("accounts", [])}
        na, nb = _nr(la2), _nr(lb2)
        check("主工作区账号已安排定时任务", bool(na.get("我的账号")), str(na))
        check("朋友工作区账号也已安排定时任务（不再只认默认工作区）", bool(nb.get("朋友的账号")), str(nb))
        print("\n[C4] 通知配置隔离")
        req(base, "PUT", "/api/global/config", tok_b,
            {"config": {"notify": {"desktop": False, "webhook_enabled": True,
                                   "webhook_type": "pushplus", "webhook_url": "friend-only"}}})
        st, cfg_a = req(base, "GET", "/api/global/config", legacy)
        st2, cfg_b = req(base, "GET", "/api/global/config", tok_b)
        check("朋友的工作区读到自己那份配置",
              cfg_b.get("notify", {}).get("webhook_url") == "friend-only", str(cfg_b))
        check("主工作区看不到朋友的配置（仍是空的）",
              cfg_a.get("notify", {}).get("webhook_url") in ("", None), str(cfg_a))

        print("\n[C8] 备份隔离（朋友的备份绝不能包含主账号数据）")
        st, zb = req_bytes(base, "/api/backup", legacy)
        zn = zipfile.ZipFile(io.BytesIO(zb)).namelist() if st == 200 and zb else []
        check("主账号能下载备份", st == 200 and len(zn) > 0, "status=%s files=%d" % (st, len(zn)))
        check("主账号备份含自己的 accounts/", any(n.replace("\\", "/").startswith("accounts/") for n in zn), str(zn[:6]))
        flat = [n.replace("\\", "/") for n in zn]
        check("主账号备份里没有别人工作区的目录（ws/…）",
              not any(n.startswith("ws/") for n in flat), str(flat[:8]))
        check("主账号备份里没有朋友的账号", not any(a2.get("id", "x") in n for n in flat), str(flat[:8]))
        check("主账号备份里没有令牌表", not any(n == "tokens.json" for n in flat), str(flat[:8]))
        st2, zb2 = req_bytes(base, "/api/backup", tok_b)
        zn2 = zipfile.ZipFile(io.BytesIO(zb2)).namelist() if st2 == 200 and zb2 else []
        flat2 = [n.replace("\\", "/") for n in zn2]
        check("朋友能下载备份", st2 == 200, "status=%s" % st2)
        check("朋友的备份里只有自己的账号",
              any(n.startswith("accounts/") for n in flat2) and not any(a1.get("id", "x") in n for n in flat2), str(flat2[:8]))
        check("朋友的备份里没有 ws/ 目录", not any(n.startswith("ws/") for n in flat2), str(flat2[:8]))
        print("\n[C5] 权限：普通令牌不能管理别人的工作区")
        st, _ = req(base, "GET", "/api/workspaces", tok_b)
        check("普通令牌列工作区 → 403", st == 403, str(st))
        st, _ = req(base, "DELETE", "/api/workspaces/default?confirm=x", tok_b)
        check("普通令牌删默认工作区 → 403", st == 403, str(st))
        st, _ = req(base, "POST", "/api/workspaces/default/rotate", tok_b)
        check("普通令牌轮换默认工作区 → 403", st == 403, str(st))
        st, _ = req(base, "POST", "/api/workspaces", tok_b, {"name": "偷偷建"})
        check("普通令牌建工作区 → 403", st == 403, str(st))

        print("\n[C6] 轮换自己的令牌")
        st, rot = req(base, "POST", "/api/workspaces/%s/rotate" % ws_b, tok_b)
        check("自己能轮换 → 200 且返回新令牌", st == 200 and rot.get("token"), str(rot)[:100])
        tok_b2 = rot["token"]
        st, _ = req(base, "GET", "/api/me", tok_b)
        check("旧令牌立即失效 → 401", st == 401, str(st))
        st, me_b2 = req(base, "GET", "/api/me", tok_b2)
        check("新令牌可用", st == 200 and me_b2.get("ws") == ws_b)
        st, _ = req(base, "GET", "/api/me", legacy)
        check("主令牌不受影响", st == 200)

        print("\n[C9] 日志隔离（朋友看不到主账号的运行记录）")
        st, lg_a = req(base, "GET", "/api/logs?n=400", legacy)
        st2, lg_b = req(base, "GET", "/api/logs?n=400", tok_b2)
        la_txt = lg_a.get("logs", "") if isinstance(lg_a, dict) else ""
        lb_txt = lg_b.get("logs", "") if isinstance(lg_b, dict) else ""
        marker = "的令牌已轮换"          # 该行是朋友轮换时产生的，只会落在朋友的工作区
        check("朋友的日志里有自己那行操作记录", marker in lb_txt, lb_txt[-80:].replace("\n", " "))
        check("主账号的日志里没有朋友那行操作记录（已隔离）", marker not in la_txt, la_txt[-80:].replace("\n", " "))
        check("主账号日志里没有朋友的账号名", "朋友的账号" not in la_txt)
        check("朋友日志里没有主账号的账号名", "我的账号" not in lb_txt)
        print("      （主日志 %d 字 / 朋友日志 %d 字）" % (len(la_txt), len(lb_txt)))
        print("\n[C7] 改名与删除（主令牌）")
        st, lst = req(base, "GET", "/api/workspaces", legacy)
        check("主令牌能列工作区", st == 200 and len(lst.get("workspaces", [])) == 2, str(lst)[:150])
        check("列表不返回完整令牌",
              all("token" not in t for t in lst.get("tokens", [])), str(lst.get("tokens"))[:120])
        req(base, "POST", "/api/workspaces/%s/rename" % ws_b, legacy, {"name": "老王"})
        st, lst = req(base, "GET", "/api/workspaces", legacy)
        nm = next((x["name"] for x in lst["workspaces"] if x["ws"] == ws_b), "")
        check("改名生效", nm == "老王", nm)
        st, _ = req(base, "DELETE", "/api/workspaces/%s?confirm=%s" % (ws_b, quote("错的")), legacy)
        check("confirm 不对 → 400", st == 400, str(st))
        st, dele = req(base, "DELETE", "/api/workspaces/%s?confirm=%s" % (ws_b, quote("老王")), legacy)
        check("删除成功且返回归档路径", st == 200 and dele.get("archived"), str(dele)[:110])
        st, _ = req(base, "GET", "/api/me", tok_b2)
        check("删除后该工作区令牌失效 → 401", st == 401, str(st))
        st, _ = req(base, "GET", "/api/me", legacy)
        check("主工作区依然可用", st == 200)
        st, la = req(base, "GET", "/api/accounts", legacy)
        check("主的账号没受影响", "我的账号" in [x["name"] for x in la.get("accounts", [])])

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
