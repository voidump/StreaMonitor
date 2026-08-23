# 健康度审计整改(Phase 0 + Phase 1)实施计划

> **For Claude:** 本计划在 `.worktrees/audit-fixes` 工作区内执行,按批次 TDD、每个逻辑改动一个提交、每批结束跑全量测试。
> 测试命令统一为:`/Users/ty/codes/_incubator/StreaMonitor/.venv/bin/python -m pytest tests/ -q`(基线 275 全绿)。
> 审计报告:claude.ai/code/artifact/1d767bd4-1e5d-452d-8483-fa5379949d63

**Goal:** 消除审计确认的全部 P0 与 Phase 0/1 范围内的 P1/P2:数据丢失链、线程/退出挂死、认证缺陷、Docker 交付链损坏、CI 不跑测试。

**Architecture:** 不做大重构(api_v2 拆分、前端架构属 Phase 2,不在本计划)。所有修复保持现有模块边界;新增两个小工具模块(原子写、无其他)。

**范围外(Phase 2,另行计划):** api_v2 Blueprint 拆分、前端局部渲染改造、密码哈希化+会话代次、README 全面重写、站点适配层统一抽象、47 提交推私有远端(需用户提供远端)。

---

## Batch 0 — 仓库卫生

### T0.1 git 卫生
- `git rm --cached history.json`;删除已提交的 `.playwright-mcp/*.yml`(git rm -r --cached + 物理删除残留)
- `.gitignore` 追加:`history.json`、`.playwright-mcp/`
- 根目录加空 `conftest.py`(裸 pytest 可跑)
- `requirements.txt` 删除 `fake-useragent`;`parameters.py:329` 删过时注释
- 验证:全量测试绿;`pytest tests/test_config_save.py -q`(不带 python -m)也绿
- Commit: `chore: 仓库卫生 — 停止追踪运行时/调试文件,删除死依赖,修复裸 pytest`

## Batch 1 — 线程与退出止血

### T1.1 log.py 幂等 handler(先做,后续测试输出干净)
- `Logger.__init__`/`get_logger`:`logging.getLogger(name).handlers` 非空则不再 addHandler
- Test `tests/test_log.py`:同名 Logger 构造两次 + get_logger 两次 → handlers 数量为 1
- Commit: `fix(log): 同名 logger 不再重复挂 handler(manager 日志重复 5 遍)`

### T1.2 移除主播时终止线程 + bot 线程 daemon 化
- `manager.py` do_remove/do_purge:`streamer.stop(None, None, thread_too=True)`
- `bot.py __init__`:`self.daemon = True`(泄漏线程不再阻塞解释器退出;清退仍由 clean_exit 显式等待)
- Test `tests/test_manager_lifecycle.py`:fake streamer 记录 stop 调用参数,do_remove 后断言 thread_too=True 且被移出列表
- Commit: `fix(manager): 删除/清除主播时真正终止 bot 线程,bot 线程 daemon 化`

### T1.3 Bot.stop TOCTOU + clean_exit 健壮化
- `bot.py stop()`:先 `self.running = False`,`cb = self.stopDownload; if cb: try cb()`
- `clean_exit.py`:每个 streamer.stop 包 try;等待循环加总超时(30s),超时记录未退出线程名后 `os._exit(1)` 兜底
- Test:fake streamer stop 抛异常不中断其余清退;超时路径用短 deadline 单测(注入超时参数)
- Commit: `fix(exit): stop 回调竞态修复;清退单点异常不再中断,加超时兜底`

### T1.4 ffmpeg 停止三段式 + stopDownload 提前赋值
- `ffmpeg.py`:`communicate(b'q', timeout=10)` → TimeoutExpired → `terminate()` → wait(5) → `kill()`;communicate 包 try(BrokenPipe 等);`self.stopDownload = ...` 移到 `thread.start()` 之前
- Test `tests/test_ffmpeg_stop.py`:假 process(poll/communicate/terminate/kill 可编程)驱动 execute 停止路径,断言超时后 terminate/kill 依次被调
- Commit: `fix(ffmpeg): 停止路径加超时与 terminate/kill 兜底,消除永久挂起`

### T1.5 cookie 更新线程单例
- `bot.py`:`self._cookie_thread` 句柄;已存活则不再新建;线程 daemon=True
- Test:模拟连续两轮 PUBLIC,断言只有一个线程(抽出 `_ensure_cookie_thread()` 便于单测)
- Commit: `fix(bot): 连续在线不再叠加 cookie 更新线程`

### T1.6 管理接口与看门狗异常保护
- `climanager.py`:execCmd 包 try;仅 tty 下 EOF 才触发 do_quit(非 tty 直接 return)
- `Downloader.py`:`if not is_docker() and sys.stdin.isatty():` 才启动 CLIManager
- `zmqmanager.py`:循环体 try/except,异常格式化为回复,保证 recv/send 配对
- `outofspace_detector.py`:run 循环体整体 try/except;space_usage 直接 try disk_usage 回退 '.'
- Test `tests/test_manager_robustness.py`:execCmd 抛异常后 ZMQ 循环继续且有 send(用假 socket);OOS 一轮抛 OSError 后线程继续(注入)
- Commit: `fix(managers): CLI/ZMQ/OOS 异常不再杀死管理线程;无 tty 不再启动 CLI`

## Batch 2 — 数据完整性

### T2.1 cache_file_list 排除 .tmp. / 隐藏文件 + 删除端点双保险
- `bot.py:181` 循环加 `if file.name.startswith('.') or '.tmp.' in file.name: continue`
- `api_v2.py` 单文件 DELETE 与全部删除:对 `'.tmp.' in filename` 直接 403/跳过
- Test 扩展 `tests/test_media_index.py` 或新增:outputFolder 放 `a.tmp.mp4` → cache_file_list 不含它;DELETE .tmp. 名 → 403
- Commit: `fix(bot): 文件列表与删除端点排除 .tmp. 临时文件,保护录制中数据`

### T2.2 temp_repair 环境错误不再删源文件
- `_repair_one` FFRuntimeError 分支:一律保留 tmp(_discard_output + warning);删除 `_cleanup_failed`
- Test `tests/test_temp_repair.py`:假 FFmpeg 抛 FFRuntimeError(exit 1)→ tmp 仍存在、输出被清;exit 255 同;成功路径 → tmp 被删
- Commit: `fix(temp-repair): ffmpeg 失败一律保留临时文件,杜绝磁盘满误删录制`

### T2.3 公共原子写 + config/history 唯一临时名
- 新增 `streamonitor/utils/atomic_file.py`:`atomic_write_json(path, data)` / `atomic_write_text(path, text, mode=None)` — NamedTemporaryFile(dir=同目录) + flush + fsync + os.replace,失败清理
- `config.py` save_config/save_history、`parameters.py` persist_recording_resolution、`online_history.py` 改用
- Test `tests/test_atomic_file.py`:写入成功/目标已存在/目录不存在报错不留垃圾;并发 20 线程写同一路径,终态是完整 JSON
- Commit: `feat(utils): 公共原子写(唯一临时名+fsync),config/history/设置存储接入`

### T2.4 Manager 并发锁
- `manager.py`:类级 `_lock = threading.RLock()`;所有 do_* 与 saveConfig 持锁(RLock 兼容嵌套如 do_restart)
- Test:并发 do_add/do_remove 100 次后 config 导出条目数一致(fake bots + monkeypatch save_config 收集)
- Commit: `fix(manager): 命令与配置保存全程持锁,消除并发丢主播`

### T2.5 parameters 持久化收敛 + .env 0600 + 控制字符防注入
- persist_download_dir/min_video_size/nsfw_enabled → 全部调 `_persist_env_key`
- `_persist_env_key`:模块级 Lock;atomic_write_text(mode=0o600);key/value 含 `\r\n\0` 抛 ValueError
- Test `tests/test_env_persist.py`:三个旧函数写后 .env 内容正确且其他键保留;权限 0600;换行值抛错
- Commit: `fix(parameters): .env 持久化去重、加锁、0600 权限、拒绝控制字符`

### T2.6 删除 WANTED_RESOLUTION 死常量
- 删 `parameters.py` 335-343(两常量+注释);grep 确认无引用
- Test:子进程 `STRMNTR_RESOLUTION=max python -c "import parameters"` 退出码 0
- Commit: `fix(parameters): 删除遗留 WANTED_RESOLUTION,STRMNTR_RESOLUTION=max 不再崩溃`

## Batch 3 — Web 认证与前端确认框

### T3.1 compare_digest 支持非 ASCII
- `httpmanager.py check_auth`:`compare_digest(username.encode(), ...encode())`
- Test:密码设为 '中文密码' 后登录成功/失败路径均不抛异常
- Commit: `fix(auth): 非 ASCII 密码不再使登录永久 500`

### T3.2 登录失败限速 + 日志
- create_app 内基于 IP 的失败计数:≥5 次失败锁 60s(封顶 300s),命中 Basic 与表单两路;失败记 warning(含 IP);成功清零;移除 sleep(1)
- Test:同 IP 连续 5 次错密码后第 6 次即便密码正确也 429/401;窗口过期后恢复;失败会写日志(caplog)
- Commit: `feat(auth): 基于 IP 的登录失败锁定与日志,替换可被并发绕过的 sleep`

### T3.3 默认密码自动强化
- `HTTPManager.run()`:若 get_webserver_password() == 'admin'(出厂默认)→ 生成 token_urlsafe(9),set+persist,logger.warning 醒目打印
- Test:monkeypatch persist,构造后调用内部 `_harden_default_password()`,断言换掉且日志包含新密码提示;显式设置过的密码不动
- Commit: `feat(auth): 出厂默认密码 admin 自动替换为随机密码并打印`

### T3.4 waitress 生产服务器
- requirements 加 `waitress`;run() 用 `waitress.serve(app, host, port, threads=16)`,ImportError 回退 app.run
- .venv 安装 waitress;手动冒烟:起服务 curl /login 200
- Commit: `feat(web): 用 waitress 替换 Werkzeug 开发服务器`

### T3.5 确认框文件数修复(modal.js)
- remove(151-152)与 batchPurge(68-69)改用 `fileCount(s)` / `sizeOf(s)`(同文件 purgeVideos 已是正确写法);detail.js:157 头部同改
- 无 JS 测试设施:改动后人工核对三处调用点 + 浏览器冒烟
- Commit: `fix(webui): 移除/批量删除确认框显示真实文件数与体积`

## Batch 4 — 录制链路可靠性

### T4.1 loadStreamers 保留失败条目
- `config.py`:模块级 `unloaded_entries` 列表;构造异常时 append 原始 dict 并保留;`save_config` 合并写回(按 site+username 去重,内存列表优先)
- Test:构造抛异常的假 site → loadStreamers 后 save_config 输出仍含该条目
- Commit: `fix(config): 启动加载失败的主播不再被静默从配置抹掉`

### T4.2 构造函数去网络化(RoomIdBot/XLoveCam/SexChatHU)
- `bot.py RoomIdBot.__init__`:room_id 解析包 try/except → 失败置 None + sc=UNKNOWN(不再 NOTEXIST);getStatus 前置惰性重解析钩子 `_ensure_room_id()`
- `xlovecam.py`:`_id` 惰性解析 + 失败返回 UNKNOWN,仅明确应答不存在才 NOTEXIST
- `sexchathu.py`:同模式(参照 fansly_live 的重试写法)
- Test:mock 网络失败 → 构造成功、getStatus 返回 UNKNOWN;网络恢复 → 解析成功
- Commit: `fix(sites): ID 解析惰性化+可重试,网络抖动不再永久停止监控`

### T4.3 HLS 状态码与 stall 上限
- `hls.py`:playlist 非 200 并入 consecutive_failures;m3u8.loads 移入 try 同计数;连续 60 轮无新分段(≈2 分钟)视为流结束
- Test `tests/test_hls_download.py`:假 session 返回 500 HTML → 计入重试而非立即 return;连续无新分段 → 有限时间内退出
- Commit: `fix(hls): playlist 错误响应进入重试而非终止录制,加无新分段 stall 上限`

### T4.4 StripChat mouflon 密钥链路
- 去掉 getMouflonDecKey 的 lru_cache(保留内部 dict 缓存,不缓存 None);_INIT_FAILED 改带时间戳退避(600s 后允许重试);auto-extract 成功后 atomic_write_json 写回缓存文件
- Test:密钥查询失败后第二次调用会重试;提取成功后缓存文件包含新键
- Commit: `fix(stripchat): 密钥查询失败不再永久缓存,初始化可退避重试,密钥落盘`

## Batch 5 — 交付链

### T5.1 STRMNTR_DATA_DIR 统一数据目录
- `parameters.py` 顶部:`DATA_DIR = os.environ.get('STRMNTR_DATA_DIR', '.')`;`data_path(name)` 助手;.env 读取、RECORDING_RESOLUTION_STORE 改走 data_path
- `config.py`(config/history)、`httpmanager.py`(.session_secret)、`online_history.py`、`stripchat.py`(mouflon 缓存)、NSFW_MODEL_PATH 默认值 → data_path
- Test `tests/test_data_dir.py`:子进程设 STRMNTR_DATA_DIR=tmp → 各路径落在 tmp 下;默认不设时行为与现状完全一致(路径字符串断言)
- Commit: `feat(paths): STRMNTR_DATA_DIR 统一运行时数据目录(默认 . 保持兼容)`

### T5.2 Dockerfile 重写(slim + 非 root + HEALTHCHECK)
- `python:3.12-slim`;apt ffmpeg;pip 装锁文件;`USER app`;HEALTHCHECK curl login;.dockerignore 补 models/、tests/、docs/、.playwright-mcp/、downloads/
- 验证:本地 `docker build .` 成功
- Commit: `fix(docker): slim 基础镜像修复构建,非 root 运行,加 HEALTHCHECK`

### T5.3 docker-compose 修正
- 去掉 config.json 单文件挂载;`STRMNTR_DATA_DIR=/data` + `./data:/data`;挂 `./models:/app/models`;密码行加醒目注释(默认自动生成)
- Commit: `fix(compose): 数据目录整卷挂载,修复配置静默丢失与模型缺失`

### T5.4 依赖锁定 + CI 测试门
- 生成 `requirements.lock.txt`(uv pip compile 或 pip freeze);新增 `requirements-dev.txt`(pytest、ruff 定版)
- `.github/workflows/build.yml`:新增 test job(pip install + ruff check --select E9,F63,F7,F82 + pytest),build-and-push `needs: test`
- Commit: `feat(ci): 测试与 lint 前置于镜像发布;依赖锁定`

### T5.5 README 针对性更新
- 补:数据目录与备份清单、默认密码行为、NSFW 模型放置、Docker 部署段修正、MIN_VIDEO_SIZE 等会删文件的开关警告
- Commit: `docs: README 补数据目录/默认密码/NSFW/删除类开关说明`

## 收尾
1. 全量测试 + 手动冒烟(起服务、登录、列表)
2. Workflow 多智能体对全部 diff 做对抗性审查,修复确认的问题
3. superpowers:finishing-a-development-branch:测试 → 本地合并 master → 合并后重跑测试(不推送)
