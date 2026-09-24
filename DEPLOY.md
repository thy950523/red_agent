# 比奇堡路人鱼服务部署文档

本文档描述如何把本服务部署到一台公网 Linux 服务器（以 Ubuntu 22.04 / Debian 12 为例，CentOS 等其他发行版命令类似）。

## 一、这个服务是什么

FastAPI 单体服务，提供三个接口和一个演示页：

| 路径 | 作用 |
| --- | --- |
| `GET /` | 演示页（demo.html） |
| `POST /match` | 上传人脸照片 → YuNet 计数人脸 → CLIP 与 40 张单鱼图排序，返回匹配结果 |
| `POST /generate` | 上传照片 + fishId → 调火山方舟 seedream 多图生图 → 返回本站图片 URL，生成结果永久留档 |
| `GET /result/{token}` | 输出已生成的图片（读 `generated_archive/`，旧 `.results/` 文件仍兼容可访问） |

模型与素材全部随仓库分发：`models/face_detection_yunet_2026may.onnx`（人脸检测）和 `images/`（40 张单鱼图）。
只有 CLIP 模型（`openai/clip-vit-base-patch32`，约 600 MB）在首次有效匹配时从 HuggingFace 下载，缓存在服务器用户目录 `~/.cache/huggingface`。

## 二、服务器要求

- 操作系统：Linux（Ubuntu 20.04+ / Debian 11+ 均可）。
- 配置：2 核 CPU、4 GB 内存、20 GB 以上磁盘。纯 CPU 推理即可，CLIP ViT-B/32 单张图编码在百毫秒量级；2 GB 内存机器在加载 torch + CLIP 后会很紧张，不建议。磁盘需给 `generated_archive/` 留出增长空间（生成结果永久留档，见第五节）。
- Python：3.10 – 3.12（`python3 --version` 确认）。
- 出网：需能访问 HuggingFace（或其镜像）下载 CLIP 模型；调用 `/generate` 需能访问火山引擎北京接口 `ark.cn-beijing.volces.com`。

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
# 国内服务器建议加镜像源加速 torch 等大包：
# pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install -r requirements.txt
```

### 2. 配置环境变量

服务读取两个环境变量：

- `ARK_API_KEY`：火山方舟密钥，**不配置则 `/generate` 返回 503**（匹配功能不受影响）。需在火山方舟控制台开通 `doubao-seedream-5-0-flash-260915` 模型。
- `HF_ENDPOINT`：国内服务器访问不了 huggingface.co 时设为 `https://hf-mirror.com`。

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
Environment=HF_ENDPOINT=https://hf-mirror.com
ExecStart=/opt/red_agent/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765 --workers 1
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

要点：

- uvicorn 绑定 `127.0.0.1`，由 Nginx 对外反代，服务本身不直接暴露公网。
- 保持 `--workers 1`：CLIP 参考向量在每个 worker 进程里各编码缓存一份，多 worker 会成倍占用内存；本服务用进程内锁保护人脸检测器，单 worker 最稳。
- 首次启动后建议先预热一次模型（见「六、验证」），避免第一个真实用户等待模型下载。

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

    # 上传照片上限与后端 20 MB 限制对齐，并留一点余量给表单编码
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

    # 安全加固（强烈建议，见第五节）
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

## 五、公网开放前的安全清单

README 中已约定，公开暴露前必须做：

1. **请求体限制**：Nginx `client_max_body_size`（上面已配）。
2. **限频**：`/match`、`/generate` 都加 `limit_req`；`/generate` 调用付费模型，建议额外按天在网关层限次。
3. **每日生成预算**：在 Nginx（`limit_req_zone` + 日志脚本）或网关侧控制每天 `/generate` 总调用次数，防止被盗刷产生费用。
4. **生成结果永久留档**：每次成功生成都会把 PNG 和对应的 JSON 元数据写入 `generated_archive/`（已被 Git 忽略），服务不会自动清理，磁盘占用只增不减——需监控该目录大小并预留空间；迁移或备份服务时必须连同此目录一起复制，否则历史生成链接会失效。旧 `.results/` 目录里的历史文件也继续保留、仍可通过 `/result/` 访问。

## 六、部署验证

```bash
# 1. 服务存活
curl -s http://127.0.0.1:8765/ -o /dev/null -w '%{http_code}\n'   # 期望 200

# 2. 首次预热：会下载 CLIP 模型（约 600 MB），在服务器上手工触发一次
curl -s -F "file=@tests/fixtures/portrait.png" http://127.0.0.1:8765/match
# 期望返回 {"match": {...}, "alternatives": [...]}

# 3. 人脸校验分支
curl -s -o /dev/null -w '%{http_code}\n' -F "file=@images/02.png" http://127.0.0.1:8765/match
# 期望 422（鱼图没有人脸）

# 4. 公网 HTTPS
curl -s -o /dev/null -w '%{http_code}\n' https://fish.example.com/
```

## 七、小组件侧同步修改

服务换成公网域名后，在小组件目录（`../red_Beechburgh/比奇堡图鉴/`）：

1. `utils/match-config.js`：`MATCH_API_URL` 改为 `https://fish.example.com/match`。
2. `project.config.json`：恢复 `urlCheck: true`。
3. 在小红书开放平台把 `https://fish.example.com` 配置为合法请求域名；发布用的图片 URL（`/result/...`）也走同一域名。
4. 开发者工具验证后，用真机回归一次「选图 → 匹配 → 生成 → 发布」全流程。

## 八、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 首次 `/match` 很久无响应 | 正在从 HuggingFace 下载 CLIP 模型；国内服务器确认 `HF_ENDPOINT=https://hf-mirror.com`，或提前手工预热 |
| `/match` 返回 500，日志有 `torch` / `cv2` 导入错误 | 依赖装进了系统 Python 而非 venv；确认 systemd 的 `ExecStart` 指向 `.venv/bin/uvicorn` |
| `/generate` 返回 503 | 未设置 `ARK_API_KEY`，或该密钥未开通 seedream 模型 |
| `/generate` 返回 502 | 火山方舟调用失败，看 `journalctl -u red-fish` 的日志定位（密钥无效、余额不足、网络不通等） |
| 小组件上传报「URL 域名不合法」 | 未在小红书开放平台配置域名，或证书无效 |
| 内存不足被 OOM 杀掉 | 升配到 4 GB，或加 swap 过渡：`sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` |
