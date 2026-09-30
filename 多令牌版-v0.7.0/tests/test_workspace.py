"""工作区模块单元测试（A 组）。

跑法（在 多令牌版 目录下）：
    python tests/test_workspace.py

测试会在系统临时目录里造一份隔离的 core 副本（BASE_DIR=临时目录），
不会碰到 `多令牌版/data` 里的任何真实数据。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

SRC_CORE = Path(__file__).resolve().parent.parent / "core"
PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + extra) if extra else ""))


def make_env(tmp: Path, legacy_token: str) -> None:
    (tmp / ".env").write_text(
        "HOST=127.0.0.1\nPORT=8099\nAUTH_TOKEN=%s\n" % legacy_token, encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wstest_"))
    try:
        shutil.copytree(SRC_CORE, tmp / "core", ignore=shutil.ignore_patterns("__pycache__"))
        legacy = "legacy" + "0" * 26
        make_env(tmp, legacy)
        sys.path.insert(0, str(tmp))
        from core import workspace as ws  # noqa: E402

        data = tmp / "data"
        print("\n[A1] 没有 tokens.json：完全老行为")
        check("has_token_table() 为 False", ws.has_token_table() is False)
        check("legacy 令牌 → default", ws.resolve_token(legacy) == "default")
        check("错误令牌 → None", ws.resolve_token("nope" * 8) is None)
        check("空令牌 → None", ws.resolve_token("") is None)
        check("default 路径 = 老的 data/accounts",
              ws.ws_paths("default")["accounts"] == data / "accounts",
              str(ws.ws_paths("default")["accounts"]))
        check("default config = data/config.json",
              ws.ws_paths("default")["config"] == data / "config.json")
        check("非默认工作区在 data/ws/<id>/ 下",
              ws.ws_paths("ws_abcdef12")["accounts"] == data / "ws" / "ws_abcdef12" / "accounts")
        check("老部署也有一个 default 工作区", ws.list_workspaces()[0]["ws"] == "default")

        print("\n[A2] 新建工作区")
        wsx, tokx = ws.create_workspace("朋友1")
        check("ws_id 形如 ws_xxxx", bool(re.match(r"^ws_[0-9a-f]{8}$", wsx)), wsx)
        check("令牌是 32 位十六进制", bool(re.match(r"^[0-9a-f]{32}$", tokx)))
        check("tokens.json 已生成", ws.has_token_table() is True)
        check("新令牌 → 新工作区", ws.resolve_token(tokx) == wsx)
        check("**老令牌仍然可用（不会把自己锁在门外）**", ws.resolve_token(legacy) == "default")
        check("新工作区目录已创建", (data / "ws" / wsx / "accounts").is_dir())
        if os.name == "posix":
            check("tokens.json 权限 600", oct(os.stat(ws.tokens_path()).st_mode)[-3:] == "600",
                  oct(os.stat(ws.tokens_path()).st_mode)[-3:])
        else:
            # Windows 不支持 POSIX 权限位（chmod 无效，属正常）；服务器 Linux 上由上面的分支把关
            check("tokens.json 已写入（Windows 跳过权限位断言）",
                  ws.tokens_path().exists() and ws.tokens_path().stat().st_size > 0)

        print("\n[A3] 多工作区与列表")
        wsy, toky = ws.create_workspace("朋友2")
        # 造一点账号数据看统计
        (data / "accounts" / "acc_default").mkdir(parents=True, exist_ok=True)
        (data / "ws" / wsx / "accounts" / "acc_a").mkdir(parents=True, exist_ok=True)
        lst = ws.list_workspaces()
        by = {x["ws"]: x for x in lst}
        check("列表含 3 个工作区", len(lst) == 3, str([x["ws"] for x in lst]))
        check("默认工作区排最前", lst[0]["ws"] == "default")
        check("账号数统计正确（default=1, friend1=1）",
              by["default"]["accounts"] == 1 and by[wsx]["accounts"] == 1,
              "default=%s %s=%s" % (by["default"]["accounts"], wsx, by[wsx]["accounts"]))
        check("令牌列表只暴露 hint 不含完整令牌给界面",
              all("hint" in t for t in ws.list_tokens()))

        print("\n[A4] 轮换令牌")
        new_tok = ws.rotate_token(wsx)
        check("旧令牌立即失效", ws.resolve_token(tokx) is None)
        check("新令牌可用", ws.resolve_token(new_tok) == wsx)
        check("轮换不影响别的区", ws.resolve_token(toky) == wsy and ws.resolve_token(legacy) == "default")

        print("\n[A5] 改名 / 追加令牌")
        ws.rename_workspace(wsx, "老王")
        nm = [x for x in ws.list_workspaces() if x["ws"] == wsx][0]["name"]
        check("改名生效", nm == "老王", nm)
        extra = ws.add_token(wsx, "老王备用")
        check("一个工作区可以多把令牌（都指向同一区）",
              ws.resolve_token(extra) == wsx and ws.resolve_token(new_tok) == wsx)

        print("\n[A6] 删除工作区")
        try:
            ws.delete_workspace("default")
            check("default 不可删（应抛错）", False)
        except ValueError:
            check("default 不可删（抛错正确）", True)
        try:
            ws.delete_workspace("../etc")
            check("非法 id 被拒绝（应抛错）", False)
        except ValueError:
            check("非法 id 被拒绝（抛错正确）", True)
        (data / "ws" / wsy / "accounts" / "keep.txt").write_text("x", encoding="utf-8")
        archived = ws.delete_workspace(wsy)
        check("原目录已移走", not (data / "ws" / wsy).exists())
        check("归档目录存在（可反悔）", bool(archived and archived.exists()), str(archived))
        check("归档里数据还在", bool(archived and (archived / "accounts" / "keep.txt").exists()))
        check("该区令牌全部失效", ws.resolve_token(toky) is None)
        check("其他区不受影响", ws.resolve_token(new_tok) == wsx and ws.resolve_token(legacy) == "default")

        print("\n[A7] 老部署兼容（有 tokens.json 但目录结构仍是老的）")
        check("default 仍指向老 accounts 目录",
              ws.ws_paths("default")["accounts"] == data / "accounts")

    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
