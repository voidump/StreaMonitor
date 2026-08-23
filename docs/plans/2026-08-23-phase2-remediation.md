# 审计整改 Phase 2 实施计划

> **For Claude:** 在 `.worktrees/phase2` 执行,分批 TDD、每批全量测试、逐改动提交,收尾做多智能体对抗审查后本地合并 master(不推送)。
> 测试命令:`/Users/ty/codes/_incubator/StreaMonitor/.venv/bin/python -m pytest tests/ -q`(基线 354 全绿)。
> **硬约束:本轮禁止改动 `webui/js/main.js` 与 `webui/js/views/dashboard.js`**(master 上有用户未提交的 WIP)。涉及这两个文件的项(dashboard 转义、播放器子树隔离、mock NOW 的 main.js 侧、ES Modules 迁移)全部顺延。

**Goal:** 完成审计报告的 Phase 2 结构性还债:认证深化、前端安全与稳健、api_v2 拆分、站点适配统一,以及可顺手消化的 P2 尾项。

## Batch A — 认证深化
- A1 密码哈希化:存 `werkzeug.security.generate_password_hash`(pbkdf2),`check_auth`/改密端点用 `check_password_hash`;.env 中检测到明文旧值启动时自动迁移为哈希;`persist_webserver_password` 持久化哈希。注意:出厂默认检测(`== 'admin'`)、空密码禁用逻辑、`_harden_default_password` 全链路适配。Test:明文迁移、哈希登录、改密、非 ASCII。
- A2 会话代次吊销:session 里存 `auth_gen`(密码哈希指纹前 8 位);改密后旧 cookie 失效;默认会话期 365→30 天。Test:改密后旧 session 401。
- A3 登录页 locked 文案:锁定分支 `redirect('/login?error=locked')`,login.html 增加对应提示。Test:锁定后 302 Location 带 error=locked。

## Batch B — 前端安全与稳健(不碰 main.js/dashboard.js)
- B1 esc() 全量补齐:modal.js 三处标题、detail.js `f.id` 系列 data-* 属性、player.js 文件名、settings.js 复查。人工核对 + node --check。
- B2 轮询契约化(store.js):删除 `n>1000?n:n*1000` 单位启发式(后端字段恒为秒,一律 *1000);refreshStreamers 加 in-flight 守卫 + 递增 token 丢弃过期响应 + AbortController 15s 超时。
- B3 快捷键单一来源:PLAYER_HOTKEY_DEFAULTS 只留 store.js 一份,player.js 引用。
- B4 vendor/mpegts.js 加 VENDOR.md(上游、版本推断、sha256、许可证说明)。

## Batch C — api_v2 拆分
- C1 `api_v2.py` → `api_v2/` 包:`__init__.py`(register_api_v2 聚合 + 兼容再导出)、`common.py`(_json、鉴权无关小工具)、`durations.py`(时长缓存+探测线程)、`thumbs.py`、`live.py`(容器解析+流式)、`branding.py`(avatar/favicon)、`streamers.py`、`videos.py`、`settings.py`、`history.py`。
- 兼容策略:tests 大量 `monkeypatch.setattr(api_v2, 'X', ...)` — 拆分后各子模块内部引用改为读模块属性(`from . import durations` 后 `durations.X`)或把补丁点集中在包命名空间;逐个跑 test_api_v2 修正 monkeypatch 目标。
- C2 顺手项:history 端点从列表下标改稳定键寻址(site+username+deletedAt,前端 store.js 相应传参);media_index 补 `is_readable()` 公开接口替代私有访问。
- 验收:161 个 API 测试全绿,路由清单与拆分前逐条一致(启动时 dump url_map 对比)。

## Batch D — 站点适配统一
- D1 Bot 基类加 `_fetch_json(method, url, **kw) -> dict|None`:统一超时、状态码检查、JSON 解析、告警日志;解析失败返回 None。
- D2 按危害排序改造:chaturbate(裸 except 误判 RATELIMIT→按异常类型区分)、streamate(`Status(r.status_code)`→显式映射)、cherrytv/camscom/bongacams/flirt4free/fansly_live(裸 r.json())。每个适配器配 mock 测试(错误响应→UNKNOWN 不抛)。
- D3 fmp4s_wss:捕获 OSError 计入有限重试、重连退避、裸 except 收窄、write 处累计字节调 update_recording_progress。
- D4 BulkStatusManager 死代码(bot_sessions/do_quit)清理或接线。

## Batch E — 尾项
- E1 media_index:duration 为 NULL 的行不再每轮全量重写(缓存区分 miss 与 null)。
- E2 nsfw_backfill.progress 改 COUNT(*) 近似,去掉逐行 os.stat。
- E3 docs/plans 旧设计文档顶部加「已过时」标注;为 NSFW 管线与回收站各写一页简短设计 note。
- E4 Web 安全小项:视频/图片响应加 `X-Content-Type-Options: nosniff`;favicon 代理不返回 svg 类型 + `CSP: sandbox`(原审计 P2#43)。

## 收尾
全量测试 → 冒烟(起服务登录+列表+播放器页)→ 对抗审查 workflow → 修复 → 本地合并 master → 合并后重跑测试。
