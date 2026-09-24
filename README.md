# 比奇堡路人鱼：最小可用版

本目录是独立的照片匹配与假生图服务；小红书小组件代码在相邻目录 `../red_Beechburgh/比奇堡图鉴/`。

## 本地运行

```bash
python3 -m pip install -r requirements.txt
HF_ENDPOINT=https://huggingface.co uvicorn app:app --host 0.0.0.0 --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`，上传包含人脸的照片。当前局域网地址为 `http://172.16.8.82:8765/`，小组件的 `MATCH_API_URL` 暂时指向 `http://172.16.8.82:8765/match`。服务先用已随项目保存的 OpenCV YuNet 模型计数人脸；当前临时规则是至少检测到一张，多张也允许进入匹配。首次有效匹配会下载约 600 MB 的 CLIP 模型；图库的 40 张单鱼图在启动后的第一次有效请求时编码并缓存在内存中。`01.jpg` 是三鱼拼图，没有作为单鱼候选。

`POST /match` 接收 `file` 表单字段。没有检测到人脸时返回 HTTP 422 和 `请上传一张包含人脸的图片`；检测到一张或多张时返回选中的鱼编号、图片地址和两个备选。检测只计数人脸，不识别用户身份；匹配是娱乐性质的图像向量排序。

小组件选择相册图片时优先请求压缩版本；后台接收上限为 20 MB。超限返回 HTTP 413 和 `照片不能超过 20 MB`，小组件会显示该具体原因。

`POST /generate` 接收 `file` 和 `fishId` 表单字段，直接把传入的照片存成 PNG 并返回 `imageUrl`，同时返回 `mock: true`。它不调用外部生图模型，也不产生模型费用。图片放在 `.results/`，地址 24 小时后失效；旧文件会在后续生成时清理。匹配接口只在内存中处理原照片，假生图接口则会暂存照片以提供 URL。

对公网开放前，应在 HTTPS 网关给 `/match` 和 `/generate` 设置请求体大小、调用频率和每日生成预算限制，避免公开生成接口被滥用。

## 小组件接入

小组件不能再跳过人脸检测。当前仅用于局域网开发：电脑与测试手机需在同一网络，电脑的局域网 IP 变化后要更新 `utils/match-config.js`；`project.config.json` 的 `urlCheck` 暂时设为 `false`。小红书文档描述 `xhs.uploadFile` 使用 HTTPS，因此 HTTP 局域网地址仍需在开发者工具和真机上分别验证。发布前应换成公网 HTTPS 域名、在开放平台配置该域名，并恢复 `urlCheck: true`。后台未检测到人脸时，小组件会在选图页提示重选；检测到人脸后才展示匹配结果，之后可通过 `/generate` 得到假生图 URL。

点击「发布」时，小组件会直接使用已有的 `generatedImage`；如果尚未生成，会先调用 `/generate`，再调用 `xhs.postNote`。后台模式需使用公网 HTTPS 图片地址；本地 `127.0.0.1` URL 仅能验证接口。

## 验证

```bash
python3 -m pytest tests/test_app.py -q
node tests/test_widget_publish.cjs
node tests/test_widget_publish_after_mock.cjs
node tests/test_widget_requires_face_check.cjs
node tests/test_widget_face_errors.cjs
```
