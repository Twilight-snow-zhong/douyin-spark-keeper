"""工作区路径隔离测试（P2a：contextvars 方案）。

验证两件事：
1. **未设置工作区上下文时 = 老行为**（data/accounts、data/config.json、data/logs）
2. **设置上下文后，账号/配置/日志各走各的目录，互不串**（隔离的关键）

跑法（在 多令牌版 目录下）：
    python tests/test_ctx_paths.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + extra) if extra else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ctxtest_"))
    try:
        shutil.copytree(SRC / "core", tmp / "core", ignore=shutil.ignore_patterns("__pycache__"))
        (tmp / ".env").write_text("AUTH_TOKEN=legacy" + "0" * 26 + "\n", encoding="utf-8")
        sys.path.insert(0, str(tmp))
        from core import ctx, config, accounts, workspace  # noqa: E402
        from core import runtime  # noqa: E402

        data = tmp / "data"
        wsx = "ws_aaaa1111"

        print("\n[B1] 未设置上下文 = 老路径（v0.6.6 行为不变）")
        check("data_dir() = data/", config.data_dir() == data)
        check("accounts_dir() = data/accounts", accounts.accounts_dir() == data / "accounts")
        check("logs_dir() = data/logs", runtime.logs_dir() == data / "logs")
        check("global_config_path() = data/config.json", config.global_config_path() == data / "config.json")
        check("order_file() = data/accounts_order.json", accounts.order_file() == data / "accounts_order.json")

        print("\n[B2] 设为工作区后，三类路径一起切过去")
        tok = ctx.set_root(workspace.ws_paths(wsx)["root"])
        check("data_dir() 切到 data/ws/<id>", config.data_dir() == data / "ws" / wsx)
        check("accounts_dir() 切过去", accounts.accounts_dir() == data / "ws" / wsx / "accounts")
        check("logs_dir() 切过去", runtime.logs_dir() == data / "ws" / wsx / "logs")
        check("global_config_path() 切过去", config.global_config_path() == data / "ws" / wsx / "config.json")
        check("ws_paths 与运行时保持一致",
              workspace.ws_paths(wsx)["accounts"] == accounts.accounts_dir())

        print("\n[B3] 真隔离：两个工作区各建一个账号，各自只能看到自己那个")
        # 默认工作区（老路径）
        ctx.reset_root(tok)
        a_default = accounts.account_dir("acc_default")
        a_default.mkdir(parents=True, exist_ok=True)
        (a_default / "config.json").write_text(json.dumps({"name": "我的账号"}), encoding="utf-8")
        # 工作区
        tok2 = ctx.set_root(workspace.ws_paths(wsx)["root"])
        a_ws = accounts.account_dir("acc_friend")
        a_ws.mkdir(parents=True, exist_ok=True)
        (a_ws / "config.json").write_text(json.dumps({"name": "朋友的账号"}), encoding="utf-8")

        ids_ws = [x["id"] for x in accounts.list_accounts()]
        check("工作区里只看到自己的账号", ids_ws == ["acc_friend"], str(ids_ws))
        # 全局通知配置也分开写
        config.save_global_config({"notify": {"desktop": False, "webhook_enabled": True,
                                              "webhook_type": "ntfy", "webhook_url": "https://ntfy.sh/wsx"}})
        ws_cfg = json.loads((data / "ws" / wsx / "config.json").read_text(encoding="utf-8"))
        check("工作区的通知配置写到自己的工作区目录",
              ws_cfg["notify"]["webhook_url"] == "https://ntfy.sh/wsx")
        check("老路径的 config.json 未被这次写入污染",
              not (data / "config.json").exists() or
              json.loads((data / "config.json").read_text(encoding="utf-8")).get("notify", {}).get("webhook_url") != "https://ntfy.sh/wsx")

        ctx.reset_root(tok2)
        ids_default = [x["id"] for x in accounts.list_accounts()]
        check("切回默认工作区只看到自己的账号", ids_default == ["acc_default"], str(ids_default))
        check("两边账号目录物理不同",
              accounts.account_dir("acc_default") != workspace.ws_paths(wsx)["accounts"] / "acc_default")

        print("\n[B4] 复位后回到老路径")
        check("reset 后 data_dir() = data/", config.data_dir() == data)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
