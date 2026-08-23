# NSFW 分析流水线(设计 note)

一句话:录制过程中定期抽帧打分,录制结束给视频贴 adult / non-adult /
unknown 标签,再按配置的动作处理 non-adult 视频。

## 数据流

```
录制中 ──每 NSFW_SAMPLE_INTERVAL 秒──▶ 抽帧 ──▶ ONNX 分类器打分(0..1)
录制结束 ─▶ 汇总分数:任一帧 ≥ NSFW_THRESHOLD ⇒ adult
                       全部帧 < 阈值           ⇒ non-adult
                       无法分析(缺模型/缺包/ffmpeg 失败) ⇒ unknown
        ─▶ 写入 media_index.classification(classification_source='auto')
        ─▶ 按 NSFW_ACTION 处理 non-adult:
             mark_only  只打标签(默认)
             recycle    移入回收站,保留 NSFW_RECYCLE_RETENTION_DAYS 天后每日清除
             delete     立即删除
```

## 不变量(改代码前先读)

- **adult / unknown / 未分析(NULL)的视频永远不会被自动移动或删除**;
  只有明确标为 non-adult 的才会被 action 处理。OOS 低磁盘清理同理。
- 人工改判写 `classification_source='manual'`,任何自动路径都不覆盖;
  改判为非 non-adult 时,已回收的文件会被还原。
- 分析不可用时一切降级为 unknown,录制永不受影响;`STRMNTR_NSFW_ENABLED`
  关闭时不 import、不加载任何模型。
- 全部 NSFW 设置是运行时可改的持久化设置:读取一律走 `parameters.get_*()`
  getter(改动经设置 API 立即生效并写入数据目录的 .env)。

## 相关模块

- `streamonitor/nsfw_analyzer.py` — 抽帧、模型加载(惰性)、打分与早停
- `streamonitor/nsfw_backfill.py` — 存量库补扫 worker(串行、可取消,
  进度 = COUNT(classification IS NULL) 近似)
- `streamonitor/media_index.py` — 标签存储、回收站移动/还原、保留期清除
- 设置 API:`api_v2/settings_api.py` 的 /settings/nsfw 与 backscan 端点

环境变量总表见 parameters.py 中 "NSFW frame analysis" 一节的注释。
