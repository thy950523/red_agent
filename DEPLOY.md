# 比奇堡路人鱼服务部署文档

本文档描述如何把本服务部署到一台公网 Linux 服务器（以 Ubuntu 22.04 / Debian 12 为例，CentOS 等其他发行版命令类似）。

## 一、这个服务是什么

FastAPI 单体服务，提供匹配、登录、额度查询与生成接口、数据统计后台，以及一个匹配演示页：

| 路径 | 作用 |
| --- | --- |
| `GET /` | 演示页（demo.html），同时下发匿名访客编号 Cookie（`vid`，仅用于统计参与人数） |
| `POST /match` | 上传真人照片 → YuNet 检测并裁切人脸 → 视觉特征与 40 条鱼资料评分 |
| `GET /auth/config` | 告诉小组件是否启用登录与额度校验，并下发「发布」预填的笔记标题/正文/话题 |
| `POST /auth/xhs` | 用小组件的一次性登录 code 换取后端会话令牌 |
| `GET /auth/quota` | 查询当前小红书用户当天的剩余生成次数 |
| `POST /generate` | 带登录令牌上传照片 + fishId → 按 openid 检查当日额度 → 调火山方舟生图并永久留档 |
| `GET /result/{token}` | 输出已生成的图片（读 `generated_archive/`，旧 `.results/` 文件仍兼容可访问） |
| `GET /admin` | 数据统计后台：统计概览、生成图片列表、CSV 导出、运行时配置（见第五节） |

模型与素材随仓库分发：`models/face_detection_yunet_2026may.onnx`、`fish_features.json`、`images/02.webp` 至 `41.webp`；原 PNG 保留给 `/generate`。

## 二、服务器要求

- 操作系统：Linux（Ubuntu 20.04+ / Debian 11+ 均可）。
- 配置：2 核 CPU、2 GB 内存起步；磁盘需给 `generated_archive/` 留出增长空间（每次成功生成会同时写入原图 PNG、320px 缩略图和 JSON 元数据，只增不减）。
- Python：3.10 – 3.12（`python3 --version` 确认）。
- 出网：匹配无需下载模型；`/generate` 需能访问火山引擎北京接口 `ark.cn-beijing.volces.com`。

## 三、部署步骤

### 1. 拉取代码并安装依赖

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
cd /opt
sudo git clone <你的仓库地址> red_agent
sudo chown -R $USER:$USER red_agent
cd red_agent

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
# 可按服务器网络情况使用 PyPI 镜像：
# pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install -r requirements.txt
```

### 2. 配置环境变量

服务读取的环境变量：

- `ARK_API_KEY`：火山方舟密钥，**不配置则 `/generate` 返回 503**（匹配功能不受影响）。需在火山方舟控制台开通 `doubao-seedream-5-0-flash-260915` 模型（模型名可在后台运行时配置里改）。
- `XHS_APP_ID`：小红书小组件 AppID（当前项目 `project.config.json` 中的 `appid`）。
- `XHS_APP_SECRET`：在小红书开放平台获取的应用密钥，只放在服务器环境变量中。缺少任一项时 `/auth/xhs` 返回 503。
- `WIDGET_AUTH_ENABLED`：默认开启；本地调试设置为 `0` 可关闭登录和生成额度校验。生产环境保持默认值。注意：后台「运行时配置」里保存过登录开关后，以后以配置为准，此环境变量不再生效。
- `GENERATION_DB_PATH`：可选，生成额度 SQLite 文件路径；默认是项目目录下 `.generation_quota.sqlite3`。部署目录必须可写，且应持久化备份。
- `RUNTIME_CONFIG_PATH`：可选，运行时配置 JSON 路径；默认是项目目录下 `runtime_config.json`（被 Git 忽略）。后台「运行时配置」保存的值写在这里，热加载、即时生效，迁移或备份时需一起复制。
- `ADMIN_PASSWORD`：`/admin` 数据统计后台的登录密码。不配置时使用内置默认密码 `xiawang123`；公网部署务必显式设置强密码覆盖默认值（也可登录后在后台「运行时配置」里设置自定义密码，自定义密码优先于环境变量）。
- 后台登录自带防爆破：同一 IP 默认 3 秒只允许一次 `/admin/login` 尝试（后台「登录限速间隔」可调，设 0 关闭），错误密码响应额外延迟 1.5 秒；它是应用层兜底，不能替代 Nginx `limit_req`。

### 3. 用 systemd 常驻运行

创建 `/etc/systemd/system/red-fish.service`（注意替换 `<用户名>` 和路径）：

```ini
[Unit]
Description=Bikini Bottom fish matching service
After=network-online.target

[Service]
User=<部署用户>
WorkingDirectory=/opt/red_agent
Environment=ARK_API_KEY=你的火山方舟密钥
Environment=XHS_APP_ID=你的小组件AppID
Environment=XHS_APP_SECRET=你的小组件AppSecret
Environment=ADMIN_PASSWORD=换成你的后台密码
ExecStart=/opt/red_agent/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765 --workers 1
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

要点：

- uvicorn 绑定 `127.0.0.1`，由 Nginx 对外反代，服务本身不直接暴露公网。
- 保持 `--workers 1`：YuNet 检测器常驻内存，并用进程内锁保护；多 worker 会分别加载模型。运行时配置按文件 mtime 热加载，手工编辑 `runtime_config.json` 也会被读到，但仍建议统一从后台修改。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now red-fish
sudo journalctl -u red-fish -f   # 看日志
```

## 四、Nginx 反向代理 + HTTPS

小红书小组件的 `xhs.uploadFile` / `xhs.postNote` 要求 **公网 HTTPS**，所以必须配域名 + 证书。

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/red-fish`：

```nginx
server {
    listen 80;
    server_name fish.example.com;      # 换成你的域名

    # 与后端上传上限对齐并留表单编码余量。
    # 后端默认 20 MB（后台「运行时配置 → 上传大小上限」可调，最大 50）；
    # 在后台调大上限时，这里的 client_max_body_size 必须同步调大，否则请求会先被 Nginx 以 413 拒掉。
    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # /generate 调用生图模型可能耗时一到两分钟
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }

    # 安全加固（强烈建议，见第六节）
    location /generate {
        limit_req zone=gen_limit burst=5 nodelay;
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_read_timeout 180s;
    }
}
```

在 `nginx.conf` 的 `http {}` 里定义限流 zone：

```nginx
limit_req_zone $binary_remote_addr zone=gen_limit:10m rate=2r/m;
```

启用并签发证书：

```bash
sudo ln -s /etc/nginx/sites-available/red-fish /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d fish.example.com   # 自动配置 HTTPS 并续期
```

同时记得在云厂商控制台**安全组放行 80/443**；不要放行 8765。

## 五、运行时配置面板

浏览器打开 `https://fish.example.com/admin`，用后台密码登录后即可在「运行时配置」里调整以下参数，保存写入 `runtime_config.json` 并立即生效，**无需重启服务**。未改过的项回落到 `runtime_config.py` 中的默认值：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 生成总开关 | 开 | 关闭后 `/generate` 不再调用生图模型，改为随机返回一张现成路人鱼图，前端流程与统计照常；匹配不受影响 |
| 小红书登录开关 | 开 | 关闭即游客模式：小组件跳过登录，`/generate` 不校验 openid 与额度 |
| 每人每天生成上限 | 5 | 按北京时间每日重置，失败不计数 |
| 上传大小上限 | 20 MB | 超限返回 413；调大时需同步调大 Nginx `client_max_body_size`（最大可设 50） |
| 会话令牌有效期 | 7 天 | 小组件登录令牌有效期 |
| 生成占位时长 | 10 分钟 | 提交后未完成的生成记录在该时长内继续占用当日额度，防止并发重复提交 |
| 后台会话有效期 | 7 天 | `/admin` 登录 Cookie 有效期 |
| 登录限速间隔 | 3 秒 | 同一 IP 两次 `/admin/login` 的最小间隔，设 0 关闭 |
| 后台密码 | 空 | 设置后优先于 `ADMIN_PASSWORD` 环境变量 |
| 生图提示词 | 「路人鱼」替换提示词 | 默认值见 `runtime_config.py`，最长 8000 字；把真人照片替换成匹配到的路人鱼形象 |
| 生图模型 | `doubao-seedream-5-0-flash-260915` | 需已在火山方舟开通对应模型 |
| 生成尺寸 | 2K | 可选 1K / 2K / 4K |
| 水印 | 关 | 传给生图模型的水印开关 |
| 人脸检测门禁 | 关 | 关闭时未检出人脸按整图兜底照常出匹配；开启后无脸/低置信度/模糊返回 422 要求重传 |
| 人脸置信度阈值 | 0.80 | 门禁开启时生效 |
| 最小人脸尺寸 | 48 px | 门禁开启时生效 |
| 清晰度阈值 | 8.0 | 门禁开启时生效 |
| 返回匹配数量 | 3 | `/match` 返回的候选数量 |
| 小红书发布预填（标题/正文/话题） | 空 | 小组件点「发布」时 `xhs.postNote` 预填的内容，经 `/auth/config` 下发；留空则不预填（平台自身限制标题约 20 字、正文约 1000 字） |

运维注意：

- `ARK_API_KEY`、`XHS_APP_ID/SECRET` 等敏感信息仍只走环境变量，不进配置面板。
- `runtime_config.json` 被 Git 忽略，迁移或备份服务时需连同 `GENERATION_DB_PATH` 的 SQLite 文件和 `generated_archive/` 一起复制。
- 手工编辑 `runtime_config.json` 也会被热加载；格式非法时该文件被忽略并整体回落默认值。

## 六、公网开放前的安全清单

README 中已约定，公开暴露前必须做：

1. **请求体限制**：Nginx `client_max_body_size`（上面已配），与后台「上传大小上限」保持联动。
2. **限频**：`/match`、`/generate` 都加 `limit_req`；`/generate` 调用付费模型，建议额外按天在网关层限次。
3. **每日生成预算**：在 Nginx（`limit_req_zone` + 日志脚本）或网关侧控制每天 `/generate` 总调用次数，防止被盗刷产生费用；应用层已有按 openid 的每日额度兜底。
4. **生成结果永久留档**：每次成功生成都会把 PNG、320px 缩略图和对应的 JSON 元数据写入 `generated_archive/`（已被 Git 忽略），服务不会自动清理，磁盘占用只增不减——需监控该目录大小并预留空间；迁移或备份服务时必须连同此目录一起复制，否则历史生成链接会失效。旧 `.results/` 目录里的历史文件也继续保留、仍可通过 `/result/` 访问。
5. **统计后台**：`https://fish.example.com/admin`，用 `ADMIN_PASSWORD`（或后台设置的自定义密码）登录后可查看参与人数、生成总数、每小时/每天统计、路人鱼人气榜和全部生成图片链接，并支持导出三个 CSV（按天汇总、按小时明细、生成记录明细，Excel 可直接打开）；统计读自 `generated_archive/` 的 JSON 留档，无独立数据库。该页面走同一个 Nginx 反代即可，无需额外配置。
6. **运行时配置面板**：见第五节，可随时热调生成开关、每日上限、上传上限、模型/提示词/尺寸、发布预填等，不必登服务器改环境变量重启。

## 七、部署验证

```bash
# 1. 服务存活
curl -s http://127.0.0.1:8765/ -o /dev/null -w '%{http_code}\n'   # 期望 200

# 2. 小组件配置接口（登录开关 + 发布预填）
curl -s http://127.0.0.1:8765/auth/config
# 期望 {"authEnabled":true,"postNote":{"title":"","content":"","tags":""}}

# 3. 真人照片匹配（模型在服务启动时加载）
curl -s -F "file=@tests/fixtures/astronaut-face.jpg" http://127.0.0.1:8765/match
# 期望返回 {"match": {...}, "alternatives": [...]}

# 4. 无真人脸图片（人脸检测门禁默认关闭，整图兜底照常出匹配）
curl -s -o /dev/null -w '%{http_code}\n' -F "file=@images/02.png" http://127.0.0.1:8765/match
# 期望 200；在后台开启人脸检测门禁后，此处才会返回 422

# 5. 公网 HTTPS
curl -s -o /dev/null -w '%{http_code}\n' https://fish.example.com/

# 6. 统计后台：未登录应返回 401，登录后能拿到 JSON
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/admin/api/summary   # 期望 401
curl -s -c /tmp/admin.cookie -o /dev/null -X POST http://127.0.0.1:8765/admin/login -d "password=你的后台密码"
curl -s -b /tmp/admin.cookie http://127.0.0.1:8765/admin/api/summary               # 期望 JSON 统计
```

## 八、小组件侧同步修改

服务换成公网域名后，在小组件目录（`../red_Beechburgh/比奇堡图鉴/`）：

1. `utils/match-config.js`：`MATCH_API_URL` 改为 `https://fish.example.com/match`。
2. `project.config.json`：恢复 `urlCheck: true`。
3. 在小红书开放平台把 `https://fish.example.com` 配置为合法请求域名；发布用的图片 URL（`/result/...`）也走同一域名。
4. 开发者工具验证后，用真机回归一次「选图 → 匹配 → 生成 → 发布」全流程。

小组件启动后调用 `xhs.login`，把一次性 `code` 发送给 `POST /auth/xhs`。后端向小红书换取 `open_id`，返回 `openid` 与自己的会话令牌；小组件在 `/match`、`/generate` 请求中传 `openid`，并带 `Authorization: Bearer <token>`。后端校验两者一致。同一 `open_id` 按北京时间每日最多成功生成 5 次（后台可调），失败不扣次数，次日重置。`GET /auth/quota?openid=...` 可查询当日剩余次数。所有生成入口都要求该令牌，因此原匿名演示页不能直接调用 `/generate`。

「发布」时小组件直接使用已生成的图片调用 `xhs.postNote`，标题、正文、话题取自后台「运行时配置 → 小红书发布预填」（启动时经 `/auth/config` 下发），后台改完后小组件下次打开即生效。

## 九、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `/match` 返回 500，日志有 `cv2` 导入错误 | 依赖装进了系统 Python 而非 venv；确认 systemd 的 `ExecStart` 指向 `.venv/bin/uvicorn` |
| `/generate` 返回 503「生图服务尚未配置」 | 未设置 `ARK_API_KEY`，或该密钥未开通 seedream 模型 |
| `/generate` 返回 502 | 火山方舟调用失败，看 `journalctl -u red-fish` 的日志定位（密钥无效、余额不足、网络不通等） |
| 上传照片返回 413 | 先被 Nginx 拦下：后台调大了「上传大小上限」但 Nginx `client_max_body_size` 没同步调大；两者需一起改 |
| 小组件「发布」时没有预填内容 | 后台「运行时配置 → 小红书发布预填」三项为空；配置后需重新打开小组件才会拉到新的 `/auth/config` |
| 后台登录提示「尝试过于频繁」 | 触发了登录防爆破限速（默认同一 IP 3 秒一次），稍等再试；可在后台「登录限速间隔」调整 |
| 后台改了配置但界面显示旧值 | 配置保存后即时生效，刷新页面即可；若手工编辑过 `runtime_config.json`，格式非法时会被忽略并回落默认值 |
| 小组件上传报「URL 域名不合法」 | 未在小红书开放平台配置域名，或证书无效 |
| 内存不足被 OOM 杀掉 | 升配到 4 GB，或加 swap 过渡：`sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` |
