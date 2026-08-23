# 回收站与保留期(设计 note)

一句话:被 NSFW 动作 `recycle` 处理的视频移入
`<download dir>/_recycle/<username> [<siteslug>]/`,保留
`NSFW_RECYCLE_RETENTION_DAYS` 天(1-90,默认 7)后由每日清除任务永久删除;
保留期内一切照常可用。

## 状态与流转

```
正常库 ──(auto 分类为 non-adult 且 action=recycle)──▶ 回收站
回收站 ──(人工改判为 adult/unknown)────────────────▶ 还原回原目录
回收站 ──(超过保留期,每日 purge)────────────────▶ 永久删除
```

## 不变量

- 回收站内的文件在保留期内**继续被列出、可播放、可出缩略图、可删除**:
  `Bot.cache_file_list` 与 `/video/` 路由把 recycleFolder 作为回退查找
  路径;前端用 `inRecycle` 标记显示徽标。
- 移动文件时缓存跟着走:`media_index._rekey_video_caches` 迁移时长缓存
  条目并改名缩略图,移动永不触发重新 ffprobe。
- 目录布局与主库同构(`<username> [<siteslug>]`),`_recycle` 相对
  download dir 解析(`STRMNTR_NSFW_RECYCLE_DIR` 可改,绝对路径原样用)。
- 回收站根目录对 NSFW 补扫、最小体积清理等自动清理是**保护路径**
  (删除策略见 nsfw-pipeline.md 的不变量)。

## 相关模块

- `streamonitor/media_index.py` — apply_nsfw_action / 还原 / 每日 purge
- `streamonitor/bot.py` — recycleFolder 属性与文件列表回退
- `api_v2/videos_api.py` — 改判端点(还原入口)
