# 抖音续火花助手 · 服务器版（Douyin Spark Keeper Server）v0.5.26

一个**自托管**的抖音"续火花"自动化工具：每天定时自动给指定好友发送一条私信，维持聊天火花（🔥）不熄灭。

**本文件夹即 v0.5.26 服务器版**：跨平台版本，同一份代码既能在 Windows 个人电脑跑，也能部署到 Linux 服务器（Debian / Ubuntu）。基于 **Python + FastAPI + Playwright + APScheduler**。

> ⚠️ 自动化发私信违反抖音社区公约，存在被风控、限流甚至封号的风险，仅限本人账号、少量好友、每天一条的个人自用场景，使用后果自负。

## 和 Windows 个人版的差异

| 项目 | 个人版（v0.5.25） | 服务器版（v0.5.26） |
|---|---|---|
| 扫码登录 | 弹出浏览器窗口（推荐）/ 网页内二维码 | **只能用「网页内二维码」**（服务器无桌面，弹窗已禁用） |
| 通知提醒 | Windows 桌面系统气泡 | **webhook 推送到手机**（ntfy / Server酱 / PushPlus） |
| 开机自启 | 网页开关（启动文件夹 VBS） | **systemd**（开机自启 + 崩溃自动重启） |
| 监听地址 | 默认 127.0.0.1 | 默认 0.0.0.0（`HOST` / `PORT` 环境变量可配） |

除上述差异外，功能与个人版完全一致：多账号、定时发送（抖动）、好友名单、历史记录、日志、备份 / 恢复、全部账号发送等。

## 功能特性（与个人版相同部分）

- 多账号管理：每个账号独立登录态 / 好友 / 定时 / 日志 / 历史，可换浏览器引擎，串行执行
- 网页内扫码登录（网页二维码模式），自动检测浏览器 / 自定义路径
- 每日定时发送：抖动窗口（0 = 准点）、好友随机间隔、失败自动补发、限流熔断
- 账号上移 / 下移排序、⚡ 全部账号立即发送 / 干跑测试
- **一键备份 / 恢复**：打包下载全部账号数据，换机一键还原
- 网页内「📖 使用说明」页（含部署教程与通知配置流程弹窗）

## 快速开始（服务器部署）

推荐使用一键脚本（Debian / Ubuntu，root 执行）：

```bash
sudo bash deploy/deploy.sh
```

自动完成：装依赖 → 装 Chromium → 建 swap → 设时区 → 生成访问令牌 → 注册 systemd 服务。
完成后访问 `http://服务器IP:8000`，令牌在 `.env`。

**完整流程见：**
- `DEPLOY_SERVER.md` —— 部署 / 管理 / HTTPS / 备份迁移 / 常见问题
- `部署教程-阿里云从零开始.md` —— 从注册服务器到跑起来的保姆级教程（新手向）
- 网页「📖 使用说明」→「🖥 服务器部署与管理」→「📖 部署教程 · 点击打开」也有完整版

### 日常管理

```bash
systemctl status douyin-spark     # 看状态
systemctl restart douyin-spark    # 重启
journalctl -u douyin-spark -f     # 实时日志
```

### 通知（webhook 推送到手机）

服务器没有桌面，请配置 webhook：「🔔 通知」→ 开启「📱 webhook 推送」→ 选服务填地址：
- **ntfy**（免注册）：`https://ntfy.sh/你的话题名`，手机装 ntfy App 订阅即可
- **Server酱**：SendKey，sct.ftqq.com 获取
- **PushPlus**：token，pushplus.plus 获取

## 快速开始（Windows 本机调试）

```bash
python app.py          # 或双击 start.bat
```

打开 `http://127.0.0.1:8000` 输入令牌（自动生成于 `.env`）。本机调试时扫码登录可用弹窗模式。

## 数据目录结构

```text
本文件夹/
├── app.py                 入口：FastAPI 网页服务（HOST/PORT 环境变量可配）
├── core/                  核心模块（自动化/定时/多账号/登录/通知/自启动等）
├── static/                网页界面（Vue3 + Element Plus，本地资源）
├── deploy/                一键部署脚本 + systemd 服务模板
├── DEPLOY_SERVER.md       服务器部署与管理文档
├── 部署教程-阿里云从零开始.md  新手部署教程
├── data/                  ⚠️ 运行数据：登录态 / 好友 / 日志（勿分享）
├── .env                   ⚠️ 访问令牌（勿分享）
└── README.md / LICENSE / requirements.txt / .gitignore
```

## 安全与合规

- 访问令牌是唯一钥匙，公网部署务必配置 HTTPS（见 DEPLOY_SERVER.md 第五章）
- `state.json` 与 `.env` 属敏感数据，切勿提交到仓库或分享（`.gitignore` 已排除）
- 自动化发私信违反抖音社区公约，仅限个人低频自用，风险自负

## License

[MIT](./LICENSE)
