# 人工标注验证集

将获授权的真人照片放在此目录，建立 `labels.jsonl`（每行一个 JSON）：

```json
{"photo":"photos/example.jpg","best":"22","alternatives":["25","41"]}
```

`best` 是标注者认为最像的鱼，`alternatives` 是也可接受的鱼。编号只允许 `02` 至 `41`。至少收集 20 张，尽量覆盖不同脸型、表情、性别、年龄、拍摄距离与光线；多人脸照片另记被选中的对象。分出从未用于调权的保留集，分别报告 top-1 和可接受备选@3。执行 `python tools/calibrate_match.py validation/labels.jsonl` 查看拟合结果；确认后加 `--write` 更新权重。脚本输出的是训练集效果，不能当作泛化准确率。

真人照片和标注文件默认不提交到 Git。
