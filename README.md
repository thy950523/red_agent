# 比奇堡路人鱼：最小可用版

本目录是独立的照片匹配与比奇堡邻居形象生成服务；小红书小组件代码在相邻目录 `../red_Beechburgh/比奇堡图鉴/`。

## 本地运行

```bash
python3 -m pip install -r requirements.txt
WIDGET_AUTH_ENABLED=0 ARK_API_KEY=火山方舟密钥 uvicorn app:app --host 0.0.0.0 --port 8765
```

本地调试用 `WIDGET_AUTH_ENABLED=0`：小组件读取 `/auth/config` 后跳过小红书登录，`/generate` 不要求 openid，也不扣用户额度。生产环境默认开启登录，需在服务器设置 `XHS_APP_ID` 和 `XHS_APP_SECRET`。该开关也可在统计后台的「运行时配置」里直接切换（保存后立即生效，并覆盖环境变量）。

浏览器打开 `http://127.0.0.1:8765/` 上传照片。项目自带 YuNet 模型和 `fish_features.json` 中 40 条可调整的鱼脸视觉特征，服务启动时加载。`images/02.webp` 至 `41.webp` 由原 PNG 素材转换，用于匹配结果展示；`01.jpg` 是三鱼拼图，不参与候选。匹配只比较选中人脸的脸型、眼睛、嘴形和表情，不使用昵称、衣服、背景或全图向量。

`POST /match` 接收 multipart/form-data 的 `file` 字段。人脸检测门禁默认关闭（后台「运行时配置」可开）：关闭时未检出人脸不再拒绝，按整图兜底照常返回匹配；开启后无脸返回 422 `NO_FACE`，人脸太小、检测置信度不足或明显模糊返回 422 `LOW_CONFIDENCE`，错误结构仍为 `{"detail":{"code":"...","message":"..."}}`。多人脸选面积最大的，面积相同时选最靠近中心的。成功返回 `{"match":{"id":"22","imageUrl":"..."},"alternatives":[...]}`。上传照片只在内存处理。

匹配接口不下载模型，也不使用随机结果。原 `ENABLE_FACE_MATCH=0` 随机模式已移除。当前初版未经人工样本验证；标注格式和校准步骤见 `validation/README.md`。

匹配响应带 `Server-Timing`，分列图片解码、人脸检测、特征提取、评分和匹配函数总耗时；服务日志也记录这些阶段。可在服务运行时执行 `python tools/benchmark_match.py --url http://127.0.0.1:8765/match --runs 100`，查看服务端与本地 HTTP 端到端 p50/p95。真实移动网络的端到端耗时应在目标设备和公网域名下另测；服务不会固定等待或在 1 秒时中断正常请求。

小组件选择相册图片时优先请求压缩版本；后台上传上限默认 20 MB（可在运行时配置里调整）。超限返回 HTTP 413 和具体原因，小组件会显示该具体原因。

`POST /generate` 接收 `file`、`fishId`、`openid` 表单字段，并要求 `Authorization: Bearer <token>`。小组件先用 `xhs.login` 获取一次性 code，提交到 `POST /auth/xhs`；后端换取 `open_id`，返回 `openid` 与自己的会话令牌，不向客户端暴露 `session_key`。小组件调用 `/match`、`/generate` 时都传 `openid`，后端核对它与会话身份一致。生成额度按 `open_id` 统计，北京时间每天默认 5 次（运行时配置可调），次日重置；失败不计次。额度记录持久化在 `.generation_quota.sqlite3`，并发请求会原子化占用额度。成功时服务将上传照片作为图一、匹配到的 `images/{fishId}.png` 作为图二，以当前配置的提示词调用火山方舟生图接口（模型与提示词默认值见 `runtime_config.py`，均可在后台调整）。服务会立即下载成图，将每次成功生成的 PNG 与生成时间、鱼编号、模型和提示词等 JSON 信息保存在 `generated_archive/`，返回本站 `imageUrl`。**留档不设自动过期或清理**；原有 `.results/` 文件也保留并继续可访问。归档目录被 Git 忽略，迁移或备份服务时需连同此目录一起复制。未配置 `ARK_API_KEY` 时返回 HTTP 503。生成会调用付费模型，并将两张参考图发送至火山引擎；匹配接口仍只在内存中处理上传照片。

对公网开放前，应在 HTTPS 网关给 `/match`、`/auth/xhs` 和 `/generate` 设置请求体大小及调用频率限制，并设置全站每日生成预算。

## 数据统计后台

浏览器打开 `http://127.0.0.1:8765/` 上传照片。项目自带 YuNet 模型和 `fish_features.json` 中 40 条可调整的鱼脸视觉特征，服务启动时加载。`images/02.webp` 至 `41.webp` 由原 PNG 素材转换，用于匹配结果展示；`01.jpg` 是三鱼拼图，不参与候选。匹配只比较选中人脸的脸型、眼睛、嘴形和表情，不使用昵称、衣服、背景或全图向量。

- **汇总卡片**：参与人数（去重访客数）、生成图片总数、人均生成、最新生成时间；
- **每小时统计**（近 48 小时）与**每天统计**（近 30 天），按北京时间展示生成张数与参与人数；
- **路人鱼人气榜**与**全部生成图片列表**（缩略图、原图链接、鱼编号、生成时间，支持复制链接和分页）；
- **数据导出**：三个 CSV 下载（UTF-8 带 BOM，Excel 可直接打开）——按天汇总（每日参与人数、生成张数）、按小时明细（每个时段的统计）、生成记录明细（每条记录的时间、鱼编号、图片链接、访客标识）；
- **运行时配置**：在后台直接调整运营参数，保存后立即生效、无需重启——生成总开关、小红书登录开关（游客模式）、每人每天生成上限（默认 5）、上传大小上限、生图提示词、生图模型、生成尺寸（1K/2K/4K）、水印开关、人脸检测门禁与人脸匹配阈值、返回匹配数量、各类会话有效期与后台密码。

密码通过环境变量 `ADMIN_PASSWORD` 配置；未设置时使用内置默认密码 `xiawang123`（适合本地和内网演示，公网部署务必用环境变量改掉，或在后台「运行时配置」里设置自定义密码——自定义密码优先于环境变量）。

参与人数按匿名访客统计：首页会下发随机编号 Cookie（`vid`，不含个人信息），生成记录同时保存该编号和客户端 IP 的截断哈希（`ipHash`），去重后即为参与人数。统计与图片列表全部读自 `generated_archive/` 的 JSON 留档，无需额外数据库。登录会话为 HMAC 签名的 7 天 Cookie（有效期可调），仅对 `/admin` 路径生效；每次生成还会保存一张 320px 缩略图（`{token}.thumb.jpg`）供后台列表加速加载。

运行时配置保存在项目目录的 `runtime_config.json`（被 Git 忽略）：只记录后台改过的项，其余回落到 `runtime_config.py` 里的默认值；手工编辑该文件也会被热加载。迁移或备份服务时请连同此文件一起复制。

## 小组件接入

小组件启动后自动调用 `xhs.login`，后端配置 `XHS_APP_ID` 和 `XHS_APP_SECRET` 才能换取 openid。人脸检测门禁默认关闭：未检出人脸时后端按整图兜底照常出匹配；在后台开启门禁后，未检测到人脸才会在选图页提示重选。当前 `utils/match-config.js` 指向公网 HTTPS 服务；需在小红书开放平台配置该合法请求域名，并在真机验证「登录 → 选图 → 匹配 → 生成」。

点击「发布」时，小组件会直接使用已有的 `generatedImage`，再调用 `xhs.postNote`。发布需使用公网 HTTPS 图片地址。

## 验证

```bash
python3 -m pytest -q
node tests/test_widget_publish.cjs
node tests/test_widget_publish_after_mock.cjs
node tests/test_widget_requires_face_check.cjs
node tests/test_widget_face_errors.cjs
```

运行时配置的后台接口与生效逻辑见 `tests/test_runtime_config.py`。
