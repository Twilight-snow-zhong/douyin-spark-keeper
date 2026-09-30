# 服务器部署指南（火花助手 · 服务器版）

本目录是**跨平台版本**：同一份代码既能在 Windows 个人电脑跑，也能部署到 Linux 服务器。
本文档只讲服务器部署；Windows 用法见 README.md。

> 和 Windows 版的三点差异：
> 1. 没有桌面环境 → 扫码登录**只能用「网页内二维码」模式**（网页里点「开始扫码登录」时会自动选好）
> 2. 没有桌面通知气泡 → 通知请配置 **webhook**（ntfy / Server酱 / PushPlus）推送到手机
> 3. 「开机自启动」开关不可用 → 由 **systemd** 管理（下方一键脚本自动配好，开机自启 + 崩溃自动重启）

> 👶 **从没部署过服务器？** 请看同目录《部署教程-阿里云从零开始.md》——
> 以阿里云轻量服务器为例，从注册账号、买服务器、连 SSH、传文件到网页打开，手把手全流程。

---

## 一、一键部署（推荐）

要求：一台 **Debian / Ubuntu** 服务器，root 或 sudo 权限，能联网。

```bash
# 1. 把整个文件夹上传到服务器（如 /root/douyin-spark）
# 2. 进入目录执行（需要 root）
sudo bash deploy/deploy.sh
```

脚本会自动完成：
- 安装 Python 3、pip、虚拟环境
- 安装项目依赖（FastAPI / Playwright 等）
- 安装 Chromium 浏览器（首次需下载几百 MB）
- 1G 内存的小服务器自动创建 2G 交换空间
- 设置时区为 Asia/Shanghai
- 生成访问令牌（保存在 `.env`，注意保管）
- 注册 systemd 服务（`douyin-spark`，开机自启、崩溃 5 秒后自动重启）

部署完成后，网页地址和访问令牌会打印在屏幕上。

---

## 二、使用步骤

1. 浏览器打开 `http://服务器IP:8000`，输入访问令牌
2. 「账号管理」→ 添加账号
3. 「概览」→「📱 网页内扫码登录」→ 选好引擎 → 点「开始扫码登录」
   - **必须用「网页内二维码」模式**（弹窗模式在服务器上不可用，界面会自动禁用）
   - 服务器上请保持设置里的「无头模式」开启
4. 「好友与消息」→ 获取聊天列表 → 勾选好友加入名单 → 填消息模板 → 保存
5. 「定时与浏览器」→ 设置每天发送时间 → 开启「参与每日定时发送」
6. 先点「干跑测试」验证流程，再点「立即发送」正式跑

---

## 三、配置手机通知（webhook）

服务器没有桌面，收不到系统气泡，请配置 webhook 把结果推到手机：

1. 打开网页 → 侧边栏「🔔 通知」
2. 开启「webhook 推送」，选服务，填地址：

| 服务 | 地址格式 | 获取方式 |
|---|---|---|
| **ntfy**（推荐，免注册） | `https://ntfy.sh/你的话题名` | 话题名自己取一个即可 |
| **Server酱** | SendKey（`sctp...`） | sct.ftqq.com 用 GitHub 登录 |
| **PushPlus** | token | pushplus.plus 微信扫码 |

3. 点「🧪 发送测试通知」验证手机能收到
4. 保存后，发送完成 / 登录态过期 / 疑似限流都会推送到手机

---

## 四、日常管理

```bash
# 查看运行状态
systemctl status douyin-spark

# 查看日志（Ctrl+C 退出）
journalctl -u douyin-spark -f

# 重启服务
sudo systemctl restart douyin-spark

# 停止服务
sudo systemctl stop douyin-spark

# 改配置（如端口/令牌）后重启
sudo systemctl restart douyin-spark
```

修改 `.env` 里的 `PORT` 可换端口；`HOST=0.0.0.0` 保持监听所有网卡。

---

## 五、配置 HTTPS（可选但推荐）

用 nginx 反向代理 + 免费证书（certbot）：

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx

# 新建 /etc/nginx/conf.d/douyin.conf：
#   server {
#     server_name 你的域名;
#     location / {
#       proxy_pass http://127.0.0.1:8000;
#       proxy_set_header Host $host;
#       proxy_set_header X-Real-IP $remote_addr;
#       proxy_read_timeout 600s;
#     }
#   }

sudo systemctl reload nginx
sudo certbot --nginx -d 你的域名   # 自动配置 HTTPS
```

之后用 `https://你的域名` 访问。不想暴露到公网的话，只开 8000 端口并用防火墙限制来源 IP 即可。

---

## 六、数据备份与迁移

- 网页「账号管理」→「⬇️ 备份全部数据」可下载 zip（登录态 / 好友名单 / 配置 / 历史全在里面）
- 换机器 / 重装后：「♻️ 恢复备份」一键还原
- 服务器上 `data/` 目录即全部数据，备份脚本前先 `sudo systemctl stop douyin-spark` 更稳妥

---

## 七、常见问题

**登录态过期？** 重新「网页内扫码登录」，或在本机（有桌面的电脑）跑 `python extract_cookie.py` 导出 state.json 后在网页上传。

**Firefox / WebKit 报错？** 服务器一般只用 Chromium（deploy.sh 只装了它）。要用其它引擎：
```bash
sudo /path/to/项目/.venv/bin/playwright install --with-deps firefox webkit
```

**内存不足 / 卡死？** 1G 内存建议启用 swap（deploy.sh 已自动做）；每次发送人数别设太高。

**想在自己电脑上管理服务器？** 两台机器数据互通：服务器「⬇️ 备份」→ 下载 → 本地「♻️ 恢复备份」，反之亦然。

---

## 八、安全提醒

- 访问令牌是唯一钥匙，**不要外泄**；部署到公网务必配 HTTPS
- `data/` 里的 state.json 等同于抖音登录凭据，备份文件妥善保管
- 本工具仅供个人自用，控制发送频率，遵守平台规则，风险自负
