# 域名 + HTTPS 部署指南（火花助手 · 服务器版）

> 适用前提：代码已经部署完（`systemctl is-active douyin-spark` 输出 `active`，`http://127.0.0.1:8000/api/health` 返回 200）。
> 还没部署代码的，先看《部署教程-阿里云从零开始.md》或 `DEPLOY_SERVER.md`。
>
> 本文记录 **2026-09-30 在阿里云 ECS（华南2·河源 / Ubuntu 22.04 / nginx 1.18）实际跑通并逐条验证过**的流程。
> 带 ✅ 实测 标记的都是当场验证过的结论，不是推测。

---

## 🚨 第 0 步：先记住这条结论（决定你后面走哪条路）

### **大陆地域的服务器：域名没备案 = 用不了。换非标准端口也救不了。**

实测对照（同一台服务器、同一张证书、同一时刻）：

| 访问方式 | 结果 | 说明 |
|---|---|---|
| `https://假域名:8443`（TLS SNI 填假域名） | ✅ 200 | 服务器、证书、nginx 全部正常 |
| `https://服务器IP:8443`（不带域名 SNI） | ✅ 200 | 同上 |
| `https://真域名:8443`（**未备案**） | ❌ **0.05 秒被 RST** | 握手阶段直接断，浏览器打不开 |
| 从**境外网络**访问 `https://真域名:8443` | ✅ 200 | 证明阻断在**国内链路**，不在机房 |

**结论**：拦截依据是 TLS 握手里的 **SNI（域名）**，**与端口无关**（8443 一样拦）。
网上流传的"用非标端口就能不备案"在大陆地域**不成立**——本文此前也这么写过，已更正。

### 三条可行路线

| 路线 | 成本 | 时间 | 说明 |
|---|---|---|---|
| ① **ICP 备案**（推荐） | 免费 | 1~3 周 | 长期方案；备案后 80/443 可用，域名 + HTTPS 全正常 |
| ② **换境外服务器**（香港/新加坡） | 约 ¥24~30/月起 | 当天 | 境外**不需要备案**，域名 + HTTPS 立刻可用 |
| ③ **纯 IP + 非标端口**（临时） | 0 | 立刻 | 先用着；代价是明文 HTTP（详见第 3 步） |

---

## 第 1 步：域名解析（三条路线都要做）

阿里云控制台 → 域名 → 「解析设置」→ 添加记录：

| 记录类型 | 主机记录 | 记录值 | TTL |
|---|---|---|---|
| A | `@` | 服务器公网 IP | 10 分钟 |
| A | `www` | 服务器公网 IP | 10 分钟 |

> 以后想给别的项目也挂子域名：再加一条 A 记录（主机记录填子域名前缀）即可；或直接填 `*` 做泛解析。

验证（在自己电脑上跑，别用本地缓存干扰）：

```bash
nslookup 你的域名 223.5.5.5
dig +short 你的域名 @223.5.5.5      # 期望输出你的服务器 IP
```

---

## 第 2 步：路线 ① —— ICP 备案（推荐，免费，1~3 周）

### 0) 备案前先自查 4 项（10 分钟，能省掉整轮白等）

1. **域名后缀是否可备案**：控制台搜「备案」→「开始备案」→ 输入域名，系统立刻判定
2. **域名实名认证已完成**：域名控制台 → 我的域名 → 状态需为「实名认证已完成」
3. **服务器是否满足备案要求**：必须是**包年包月**（按量付费不能备案），且剩余时长够；控制台 ECS → 左侧「备案」→ 申请**备案服务号**
4. **一致性铁律**：域名实名认证的**持有人姓名 + 证件号**，必须与备案主体**完全一致**（不一致要先做域名过户/实名变更）

### 1) 提交入口

控制台搜「备案」→ `beian.aliyun.com` → 「我的备案」→ **免费自助备案** → 点「**开始备案**」（传统表单，逐项可核对，别选"万小智备案助手"那种 AI 对话式）

**第 1 屏 · 互联网信息服务校验**
- 服务类型：`网站`
- 网站域名：`你的域名`（**不要带 www**）
- 主办者信息：**地区**（需与证件住所**同省同市**，不一致会被质疑）、**备案性质 = 个人**、证件类型 = 居民身份证、主办者名称 / 证件号码 / 证件住所
- 点「信息校验」→ 会校验：后缀是否支持、域名实名是否一致、是否有可用云服务实例（✅ 实测通过会显示"云服务可用性：通过校验，符合备案要求"）

**第 2 屏 · 主办者基础信息 + 负责人信息**
- 通信地址：**要具体到街道/门牌号**（照证件住所写最稳）
- 「备注」保留系统默认那段个人承诺语，别删
- 手机号必须是**绑定阿里云账号**的那个（后续工信部核验短信发到它）、验证码、应急手机号、邮箱

**第 3 屏 · 网站信息**（最容易驳回的屏）
- **网站名称**：❌ 不能含商标（如"抖音"）、不能用「XX网 / XX平台 / XX论坛 / XX门户」
  ✅ 建议：`个人学习记录`、`火花助手`、`我的小工具`
- **网站内容**：选「其他」→ 选了它**必须在备注写 ≥20 字的用途说明**，例如：
  `本网站为个人自用的消息提醒与记录工具，用于记录日常好友消息并做定时提醒，仅本人使用，不对外开放注册，不涉及任何经营性内容。`
- 网站语言：中文简体；前置审批：不涉及
- **云服务 = ECS → 云产品实例选你那台**（必填）

**第 4 屏 · 上传资料**
- **负责人资料**：身份证正反面（清晰、四角完整、不反光、必须是**最新有效期**的那张、单张 < 4MB）+ **人脸核验**（手机装「阿里云」App 扫码）
- **互联网备案信息真实性承诺书**：点「下载模板」→ **打印** → **黑色中性笔手写正楷签名** + 填身份证号 + 写签署日期 → **拍照上传**
  （必须是纸质手签后拍照；家里没打印机就去打印店，1~2 元）
- 「辅助资料」为非必传项，可跳过

**第 5 屏 · 提交订单**

### 2) 提交后的三个关键节点

| 节点 | 时间 | 注意 |
|---|---|---|
| 阿里云初审 | 1~2 个工作日 | 会**打电话**核实（陌生号码也要接） |
| **工信部短信核验** | 初审通过后**立刻** | 短信里有核验链接，**24 小时内必须点开完成**，否则备案作废 ← 最容易翻车 |
| 广东/各省管局审核 | 3~7 个工作日（最长 20） | 保持手机畅通 |

### 3) 备案通过后（5 分钟收尾）

1. 安全组放行 **TCP 80 / 443**（来源 `0.0.0.0/0`）
2. nginx 放 443 配置 + 复用现有域名证书（见第 4 步）
3. 访问地址升级为 `https://你的域名`（标准 443，不用带端口）
4. 把临时的 8000/8443 关掉：安全组删掉 8000 规则，服务改回 `HOST=127.0.0.1`

---

## 第 3 步：路线 ③ —— 纯 IP 直连（备案没下来时的临时方案）

**这是"立刻能用"的方式**，代价是明文 HTTP（令牌在公网明文传输）。功能上完全一样。

**服务器上：**

```bash
cd /root/douyin-spark
sed -i 's/^HOST=.*/HOST=0.0.0.0/' .env
systemctl restart douyin-spark
sleep 3
systemctl is-active douyin-spark
curl -s -o /dev/null -w '本机自检: %{http_code}\n' http://127.0.0.1:8000/api/health   # 期望 200
```

**阿里云控制台**：ECS → 实例 → 「网络与安全组」→ 安全组 ID → 「入方向」→「手动添加」：
自定义 TCP / `8000/8000` / `0.0.0.0/0`（**记得把自动带出的 `HTTP(80)` 标签删掉**）

**访问**：`http://服务器IP:8000`（浏览器直接手打地址；夸克等浏览器有时会把裸地址当搜索词，用 Edge/Chrome 更省事）

> ⚠️ 临时方案的三条纪律：
> 1. 8080/8000 这类端口**只开你要用的那个**，用完/备案后立刻删规则
> 2. 令牌就是唯一钥匙，别外发；日志里不打印令牌是刻意设计
> 3. 想避免明文，只能走备案或境外服务器——HTTPS + 未备案域名在大陆会被 SNI 掐断

---

## 第 4 步：nginx + HTTPS（备案通过后 / 境外服务器）

### 1) 把服务收回只允许内网

**为什么必须做**：程序判断"是否本机"看客户端 IP，而"本机"允许**无令牌**设置访问令牌（首次引导用）。
8000 若还对公网开放，别人可直连绕过 nginx。配合 nginx 传 `X-Real-IP`，程序才能识别真实来源（两者配套，见文末附录）。

```bash
cd /root/douyin-spark
grep -q '^HOST=' .env || echo 'HOST=127.0.0.1' >> .env
sed -i 's/^HOST=.*/HOST=127.0.0.1/' .env
systemctl restart douyin-spark
systemctl is-active douyin-spark
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/health   # 期望 200
```

### 2) 装 nginx（⚠️ 有个必须知道的坑）

```bash
apt-get install -y nginx
nginx -v                       # Ubuntu 22.04 装的是 1.18.0
```

> **坑（实测踩过）**：`http2 on;` 是 nginx **1.25+** 的新语法，1.18 直接报
> `nginx: [emerg] unknown directive "http2"` 并**启动失败**。
> 正确写法是把 http2 挂在 listen 后面：`listen 443 ssl http2;`（1.18 ~ 1.24 通用）。
> 本仓库 `deploy/nginx-douyin-spark*.conf` 两个模板已按此修正。

### 3) 申请证书

#### 路线 A：阿里云免费证书（✅ 实测全流程，90 天有效期）

1. 控制台搜「数字证书管理服务」→ 「**个人测试证书（原免费证书）**」→ 购买（**¥0**，每年 20 张额度，单张 90 天）
2. 「证书管理」→ 找到状态「待申请」的那条 → **「申请证书」**
3. 表单：
   - 证书绑定域名：`你的域名`（不带 www；选 DNS 验证时通常会自动赠送 `www.你的域名`）
   - 域名验证方式：**自动DNS验证**（80 端口没开时用不了文件验证）
   - 联系人：先选/新建（要真实姓名 + 手机 + 邮箱，会提交给 CA）
   - 所在地、密钥算法 `RSA_2048`、**CSR = 系统生成**（别选手动填写）
4. 提交后若跳到 RAM「**访问控制快速授权**」页（三项全绿）→ 点「返回控制台」。这一步是让证书服务有权限**自动往你的云解析加验证记录**
5. 进入「验证信息」页：显示绿色「域名验证成功」即通过；平均 **5~6 分钟**签发（状态变绿色「**已签发**」）
6. 勾选该证书 → **「下载」** → 服务器类型选 **Nginx** → 得到 `.pem` + `.key`

**传到服务器**（在自己电脑的 PowerShell 里，顺手改名成 `cert.pem` / `cert.key` 减少路径坑）：

```powershell
ssh root@服务器IP "mkdir -p /etc/nginx/ssl"
scp "本地路径\xxx.pem" root@服务器IP:/etc/nginx/ssl/cert.pem
scp "本地路径\xxx.key" root@服务器IP:/etc/nginx/ssl/cert.key
```

**校验文件没传坏**（服务器上）：

```bash
ls -l /etc/nginx/ssl/
head -1 /etc/nginx/ssl/cert.pem    # 期望 -----BEGIN CERTIFICATE-----
head -1 /etc/nginx/ssl/cert.key    # 期望 -----BEGIN PRIVATE KEY----- 或 BEGIN RSA PRIVATE KEY
```

> 这张证书**覆盖 `你的域名` 和 `www.你的域名`**；90 天后到期，到控制台重新申请一次、替换这两个文件、`systemctl reload nginx` 即可。

#### 路线 B：Let's Encrypt（自动续期，但 DNS 验证要手动配合）

```bash
apt-get install -y certbot
certbot certonly --manual --preferred-challenges dns \
  --agree-tos --register-unsafely-without-email \
  -d 你的域名 -d www.你的域名
```

它会停下并打印一条 `_acme-challenge` 的 **TXT 记录**：去云解析添加（类型 TXT / 主机记录 `_acme-challenge` / 记录值那一长串），
**等 1~2 分钟让记录生效**（可用 `nslookup -type=TXT _acme-challenge.你的域名 223.5.5.5` 确认），再回终端按回车。
签好后证书在 `/etc/letsencrypt/live/你的域名/`。缺点是**续期要重做一次 TXT**（想全自动需配 DNS API 插件）。

### 4) 放 nginx 配置

**A. 已备案、走标准 443（推荐）**

```bash
cp /root/douyin-spark/deploy/nginx-douyin-spark.conf /etc/nginx/conf.d/douyin-spark.conf
sed -i 's/your-domain\.com/你的域名/g' /etc/nginx/conf.d/douyin-spark.conf
# 证书路径改成你上传的两个文件
sed -i 's|/etc/letsencrypt/live/[^;]*fullchain.pem|/etc/nginx/ssl/cert.pem|'  /etc/nginx/conf.d/douyin-spark.conf
sed -i 's|/etc/letsencrypt/live/[^;]*privkey.pem|/etc/nginx/ssl/cert.key|'    /etc/nginx/conf.d/douyin-spark.conf
nginx -t && systemctl enable --now nginx && systemctl is-active nginx
```

**B. 非标端口 8443（仅在"已备案但 443 被占用"或"境外服务器"时用）**

```bash
cp /root/douyin-spark/deploy/nginx-douyin-spark-8443.conf /etc/nginx/conf.d/douyin-spark.conf
sed -i 's|/etc/nginx/ssl/your-domain.com.pem|/etc/nginx/ssl/cert.pem|'  /etc/nginx/conf.d/douyin-spark.conf
sed -i 's|/etc/nginx/ssl/your-domain.com.key|/etc/nginx/ssl/cert.key|'  /etc/nginx/conf.d/douyin-spark.conf
sed -i 's/your-domain\.com/你的域名/g'                                   /etc/nginx/conf.d/douyin-spark.conf
nginx -t && systemctl enable --now nginx && systemctl is-active nginx
curl -sk -o /dev/null -w '本机 https 自检: %{http_code}\n' https://127.0.0.1:8443/api/health -H 'Host: 你的域名'   # 期望 200
```

（记得安全组放行对应端口；`nginx -t` 若报 `unknown directive "http2"`，见上面第 2 步的坑。）

---

## 第 5 步：手机通知（服务器没有桌面，必须配 webhook）

网页 → 左下角 **「🔔 通知」**：

| 服务 | 地址 / Key | 特点 |
|---|---|---|
| **PushPlus**（推荐） | `pushplus.plus` 微信扫码登录 → 复制 **token** | 国内、快（0.28s）、推微信、不用装 App |
| **Server酱** | `sct.ftqq.com` 用 GitHub 登录 → 复制 **SendKey** | 国内、快（0.33s）、推微信 |
| **ntfy** | 自取话题名，如 `dsp-x7k2m9`，填 `https://ntfy.sh/话题名` | 免注册；手机装 ntfy App 并订阅同名话题（境外服务，1.7s） |

**步骤**：打开「启用」开关 → 选服务 → 填地址/Key → **先点「保存」** → 再点「🧪 发送测试通知」。
期望弹窗出现 `webhook（pushplus）✓`，微信收到「PushPlus 推送加」的消息（没关注过就搜这个公众号关注一下）。

### ⚠️ 两个实测踩过的坑

1. **「测试」读的是"已保存的配置"，不是输入框里的内容** —— 填完不点保存，测试等于没配。
2. **配置保存后被清空的 bug（v0.6.5 及更早的服务器版有；v0.6.6 已修）**
   - 根因：`core/config.py` 的 `DEFAULT_GLOBAL_CONFIG["notify"]` 只有 `desktop`，`load/save_global_config()` 也只搬 `desktop` → webhook 三项**读写一次就丢**
   - 误导表现：点「保存」提示成功、点「测试」还回**绿色"通知功能正常"**
   - **一眼判定中招**（在自己电脑上跑，令牌填 `.env` 里的）：
     ```powershell
     curl.exe --noproxy "*" -s "http://服务器IP:8000/api/global/config" -H "X-Auth-Token: 你的令牌"
     ```
     只返回 `{"notify":{"desktop":false}}`（没有 `webhook_*` 三项）就是中招 → 升级到 v0.6.6
   - 修复后应返回四项：`{"desktop":false,"webhook_enabled":true,"webhook_type":"pushplus","webhook_url":"..."}`

---

## 第 6 步：验证清单

```bash
# 服务器上
systemctl is-active douyin-spark nginx        # 都是 active
curl -s  -o /dev/null -w 'http  %{http_code}\n' http://127.0.0.1:8000/api/health
curl -sk -o /dev/null -w 'https %{http_code}\n' https://127.0.0.1:8443/api/health -H 'Host: 你的域名'
```

```powershell
# 自己电脑上（外网视角，--noproxy "*" 避免被本机代理干扰）
$u = "https://你的域名"      # 或 http://服务器IP:8000
curl.exe --noproxy "*" -s -o NUL -w "首页 HTTP=%{http_code} 耗时=%{time_total}s`n" "$u/"
curl.exe --noproxy "*" -s -o NUL -w "无令牌应 401: %{http_code}`n" "$u/api/accounts"
curl.exe --noproxy "*" -s -o NUL -w "带令牌应 200: %{http_code}`n" -H "X-Auth-Token: 你的令牌" "$u/api/accounts"
```

网页里再走一遍：令牌登录 → 账号管理 → 网页内二维码扫码 → 好友与消息配好 → **先「干跑测试」** → 再「立即发送」。

---

## 第 7 步：常见问题

| 现象 | 原因与处理 |
|---|---|
| 域名打不开、但用 IP 能打开 | **域名未备案被 SNI 阻断**（第 0 步）。备案或换境外服务器；临时用 IP |
| 浏览器报 `ERR_CONNECTION_RESET` / 秒断 | 同上（TLS 握手被 RST），不是服务器故障 |
| `unknown directive "http2"` | nginx 1.18 不认 `http2 on;` → 改成 `listen <port> ssl http2;` |
| 域名打开是「未备案」提示页 | 大陆地域 80/443 的备案拦截 |
| `502 Bad Gateway` | 应用没在跑或端口不对：`systemctl status douyin-spark`、`curl 127.0.0.1:8000/api/health` |
| `413 Request Entity Too Large` | 传的图片太大：调大 nginx `client_max_body_size`（模板里是 64m） |
| 手机浏览器提示「不安全」 | 证书域名与访问域名不一致（用 IP 访问 https、或多加/少加 www） |
| 定时发送时间不对 | 时区：`timedatectl set-timezone Asia/Shanghai`（deploy.sh 已设） |
| 反复 401 | 令牌填错；或在公网改过令牌后网页还存着旧的 |
| 改了 `.env` 不生效 | 必须 `systemctl restart douyin-spark` |
| 测试通知显示绿色"功能正常"但手机没收到 | 老版本的假成功 bug（见第 5 步）；先确认配置真的存进去了 |
| 上传文件后中文文件名变乱码（服务器上） | 服务器 locale 为 `C` 时 `unzip` 会用 CP866 解码 UTF-8 文件名 → 改用 `python3 -c "import zipfile;zipfile.ZipFile('x.zip').extractall('.')"`，或先写 `LANG=C.UTF-8` |

---

## 第 8 步：上线后的安全清单

- [ ] 访问令牌 ≥16 位随机串（`.env` 或网页里改；改完重启）
- [ ] 8000/8443 等临时端口**不对公网开放**（只留 nginx 需要的端口）
- [ ] **不要把域名/令牌发给别人**；令牌是唯一钥匙
- [ ] 「🔔 通知」配好 webhook（服务器无桌面，通知只能走手机推送）
- [ ] 定期备份：网页「⬇️ 备份全部数据」或直接打包 `data/`
- [ ] 可选：`apt-get install -y fail2ban` 防暴力扫描
- [ ] 可选：`apt-get install -y unattended-upgrades` 自动安全更新
- [ ] 备案通过后：到 [全国互联网安全管理服务平台](https://beian.gov.cn) 做**公安联网备案**（部分地区要求开通后 30 日内）

---

## 附 A：本次部署踩过的坑（按踩到的顺序）

| # | 坑 | 症状 | 解法 |
|---|---|---|---|
| 1 | 服务器 locale 为 `C`，`unzip` 把中文文件名解成乱码 | `含 续火花.png: False` 之类 | 用 Python `zipfile` 解压；或先 `LANG=C.UTF-8` |
| 2 | 用 SSH 隧道访问，隧道静默失效 | 页面一直转圈/打不开，但 ssh 进程还在监听 | 改用直连（第 3 步）或重建隧道 |
| 3 | nginx 1.18 不认 `http2 on;` | `nginx -t` 报 unknown directive，服务起不来 | 改 `listen <port> ssl http2;` |
| 4 | **未备案域名非标端口也被拦** | 域名秒断、IP 正常 | 备案 / 境外服务器 / 用 IP（第 0 步） |
| 5 | 通知配置保存后丢失（老版本 bug） | 保存成功、测试"成功"、手机没消息 | 升级 v0.6.6；用 `GET /api/global/config` 判定 |
| 6 | 关弹窗/切单选会取消正在工作的扫码会话 | 二维码一闪就没 | 服务器上用「网页内二维码」模式；开始后别点其它控件 |
| 7 | 夸克浏览器把裸地址当搜索词 | 地址栏输入后停在浏览器首页 | 用 Edge/Chrome，或输入完整 `http://` |

---

## 附 B：本次部署涉及的代码修复（重要）

### 1) 反代下的"真实来源"识别（v0.6.5 修复）

原代码判断"是否本机"用 TCP 对端地址；套上 nginx 反代后对端恒为 `127.0.0.1`，
于是**公网任何人都会被当成"本机"，可以无令牌改掉访问令牌**。

修复：`app.py` 的 `_client_ip()` —— 只在 TCP 对端确实是回环时，才采信 nginx 传来的 `X-Real-IP` / `X-Forwarded-For`：

- nginx 反代进来的公网访客 → 识别为远程，改令牌需旧令牌 ✅
- 外部直连并伪造 `X-Real-IP: 127.0.0.1` → 对端非回环，伪造无效 ✅
- 服务器本机（SSH 隧道 / 调试）→ 仍可无令牌首次设置 ✅

**部署前请确认服务器上的代码包含该修复**（v0.6.5 及之后）。

### 2) 通知配置丢失（v0.6.6 修复）

`core/config.py` 的 `DEFAULT_GLOBAL_CONFIG["notify"]` 补齐四项 + 新增 `_norm_notify()`：

```python
"notify": {
    "desktop": True,
    "webhook_enabled": False,
    "webhook_type": "ntfy",     # ntfy | serverchan | pushplus
    "webhook_url": "",
}
```

### 3) 测试通知不再误报成功（v0.6.6 修复）

后端无渠道可用时返回明确说明；前端 `results` 为空时显示**黄色警告**并提示"先点保存"，
服务器上不再提示"看电脑右下角气泡"。

---

## 附 C：最小更新法（只换几个文件，不重装）

服务器已部署、只想把版本升上来时，**只需 scp 改动的文件**再重启，数据/登录态/配置都不受影响：

```powershell
$ip = "root@你的服务器IP"
scp "本地\服务器版\app.py"              "${ip}:/root/douyin-spark/app.py"
scp "本地\服务器版\core\config.py"      "${ip}:/root/douyin-spark/core/config.py"
scp "本地\服务器版\core\notify.py"      "${ip}:/root/douyin-spark/core/notify.py"
scp "本地\服务器版\static\index.html"   "${ip}:/root/douyin-spark/static/index.html"
```

```bash
# 服务器上
cd /root/douyin-spark
python3 -m py_compile app.py core/config.py core/notify.py && echo "语法 OK"
systemctl restart douyin-spark
sleep 3
curl -s -o /dev/null -w '自检: %{http_code}\n' http://127.0.0.1:8000/api/health
curl -sk -o /dev/null -w 'https: %{http_code}\n' https://127.0.0.1:8443/api/health -H 'Host: 你的域名'
```

> 覆盖前先备份：`cp app.py app.py.bak`（或先 `tar czf /root/backup-$(date +%F).tgz /root/douyin-spark`）。
