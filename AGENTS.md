# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, etc.) working in this repository.
This file mirrors `CLAUDE.md`; edit both when architecture changes.

## 项目概述

使用**火山引擎（ByteDance）豆包 TTS API** 将文本合成语音。两套入口共享同一套核心逻辑：

- **CLI**：`custom_tts.py`，批量处理 `inputs/` 下的 `.docx`，按段落切分调用 TTS 后合并为 MP3。
- **Web 应用**：`webapp/`，FastAPI + Jinja2 单页，受访问码限制；支持 `.docx` / `.txt` / 粘贴文本输入，输出合并后的 MP3。

API 文档：https://www.volcengine.com/docs/6561/1257584

## 协议铁规（火山引擎要求，**勿"修正"**）

`tts_core.synthesize_text` 内的两条字面量是火山引擎的协议约定，任何修改都会立刻导致 401 / 业务错误码：

1. 请求体 `app.token` 必须是**字面量字符串** `"access_token"`，**不是** `cfg.access_token`。
2. Header 形如 `Authorization: Bearer;{cfg.access_token}`（**分号** + token，**不是空格**）。

## 目录结构

```
huoshanTTS2.0/
├── inputs/                              # CLI 待处理 .docx
├── .env                                 # 凭证 + 访问码（不入库，参考 .env.example）
├── Dockerfile                           # webapp 容器；装 ffmpeg + 单 worker
├── .dockerignore
└── CascadeProjects/windsurf-project/
    ├── tts_core.py                      # 纯函数 + TTSConfig（CLI 和 webapp 共用）
    ├── custom_tts.py                    # CLI 入口；负责 .env 加载、文件 IO、进度打印
    ├── requirements.txt
    ├── outputs/{YYYYMMDD_HHMMSS}/       # CLI 输出
    └── webapp/
        ├── __init__.py                  # 包初始化时加载 .env
        ├── main.py                      # FastAPI app、lifespan、路由、中间件
        ├── jobs.py                      # Job、JOBS、permit、cleanup、run_job
        ├── auth.py                      # ACCESS_CODES 解析 + 校验
        ├── ingest.py                    # 上传/粘贴 → segments，三重限额
        ├── templates/index.html         # 单页 UI
        └── static/style.css
```

`CascadeProjects/windsurf-project/README.md` 历史遗留、已过时（描述 AK/SK 鉴权）。以本文件为准。

## 常用命令

```bash
# 安装依赖
pip install -r CascadeProjects/windsurf-project/requirements.txt

# --- CLI ---
python3 CascadeProjects/windsurf-project/custom_tts.py                  # 自动取 inputs/ 下第一个 .docx
python3 CascadeProjects/windsurf-project/custom_tts.py inputs/x.docx    # 指定文件

# --- Webapp 本地开发 ---
# 注意：--reload 与 --workers 互斥；本地用 --reload，Docker 才加 --workers 1
cd CascadeProjects/windsurf-project
uvicorn webapp.main:app --reload --port 8000

# --- Webapp Docker（从仓库根目录执行）---
docker build -t huoshan-tts .
docker run -p 8000:8000 --env-file .env huoshan-tts
```

可选安装 `ffmpeg`（配合 `pydub`）以在合并时插入段落间静音（默认 500ms）；未安装时回退为 MP3 二进制拼接（无静音）。Docker 镜像内置 ffmpeg。

## 凭证与环境变量

`.env` 通过 `python-dotenv` 自动加载（从脚本目录向上查找最多 5-6 层）。亦可直接用环境变量。

| 变量 | 必需 | 说明 |
|------|------|------|
| `VOLCENGINE_APPID` | 是 | 火山引擎应用 ID |
| `VOLCENGINE_ACCESS_TOKEN` | 是 | 访问令牌（仅在服务端，不下发前端） |
| `VOLCENGINE_VOICE_TYPE` | 是 | 自定义音色 ID |
| `VOLCENGINE_CLUSTER` | 否 | 集群，默认 `volcano_icl` |
| `INPUTS_DIR` | 否（CLI） | 覆盖 `inputs/` 自动查找 |
| `ACCESS_CODES` | **是（webapp）** | 邀请码列表，逗号分隔。**空集启动失败**。值会被 `strip()`，输入校验也会 strip |
| `MAX_TOTAL_CHARS` | 否 | 单次提交总字符上限，默认 20000 |
| `MAX_UPLOAD_BYTES` | 否 | 上传文件字节上限，默认 5 MB |
| `MAX_SEGMENTS` | 否 | 切分后片段数上限，默认 200（防御极端短句拆分，非容量预估） |
| `MAX_TEXT_CHARS` | 否 | 单片段最大字符数，默认 300 |
| `MAX_CONCURRENT_JOBS` | 否 | 全局并发任务数，默认 2 |
| `JOB_TTL_SECONDS` | 否 | 任务终态后保留时长，默认 3600 |
| `JOB_MAX_RUNTIME_SECONDS` | 否 | 单任务最大运行时长（超过转 `timeout`），默认 1800 |
| `RATE_LIMIT_PER_MINUTE` | 否 | 单 IP 每分钟提交次数，默认 3 |

## Webapp 架构关键点

**并发模型：**
- `run_job` 是**同步**函数，跑在 FastAPI BackgroundTasks 默认线程池里。
- 并发限流用 **`threading.BoundedSemaphore`**（不是 `asyncio.Semaphore`）。
- POST 入口立即非阻塞 `acquire`；获取成功则 permit 绑定到 Job，由 `run_job` 的 finally 通过原子 `release_job_permit(job)` 释放。错误路径在创建 Job 前直接 `SEMAPHORE.release()`。
- **MVP 仅支持单 worker、单实例。** 多 worker / 多实例部署会让轮询打到错的进程。升级路径见 §"后续可演进"。

**鉴权：**
- 访问码：`X-Access-Code` 头；服务端 strip 后与解析后的 `ACCESS_CODES` 集合比对。
- 任务 token：创建任务时返回一次性 `job_token`；后续 `GET /api/jobs/...` 与 `/download` 都必须带 `X-Job-Token`，用 `secrets.compare_digest` 比对。无效一律 404（不暴露存在性）。
- 下载路径**不接受** URL 查询 token，前端用 `fetch` + Blob 触发下载。

**任务状态机：** `pending` → `running` → 终态之一：`done` / `done_with_warnings`（部分片段失败仍可下载）/ `error` / `timeout`。终态时设置 `expires_at = now + JOB_TTL_SECONDS`、清空 `segments`、释放 permit。

**清理：** `lifespan` async loop 每 60 秒先 `cleanup_stuck()`（运行超 `JOB_MAX_RUNTIME_SECONDS` → `timeout`）再 `cleanup_expired()`（过期终态任务删 tmp_dir、pop JOB、幂等释放 permit）。

**敏感数据保护：** `TTSConfig.access_token`、`Job.token`、`Job.segments` 都用 `field(repr=False)`，防止 `repr()` / 日志意外泄漏。审计日志只记 `sha256(access_code)[:12]`、字数、片段数、状态。

## API（webapp）

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| `GET` | `/` | — | HTML 单页 |
| `GET` | `/health` | — | PaaS 健康检查 |
| `POST` | `/api/jobs` | `X-Access-Code` | 表单 `source_type=file\|text` + `file` 或 `text`；返回 `{job_id, job_token}` |
| `GET` | `/api/jobs/{id}` | `X-Job-Token` | 状态轮询 |
| `GET` | `/api/jobs/{id}/download` | `X-Job-Token` | 下载 MP3（终态为 `done` / `done_with_warnings` 时） |

错误码：401（访问码错）/ 400（输入空、扩展名错、编码错）/ 413（字数 / 字节 / 段数超限）/ 429（速率或并发饱和）/ 404（任务不存在 / token 错）/ 409（任务未完成）/ 410（结果已被清理）。

## 核心模块要点

- **`tts_core.py`** 是纯函数 + `TTSConfig` dataclass，**不**读环境变量。所有诊断走 `logging`，由调用方决定输出。
- **`custom_tts.py`** 保留原 CLI 行为：`.env` 加载、参数解析、`inputs/` 自动发现、进度 `print`、`outputs/{run_id}/segment_NNN.mp3` + `{name}_合并.mp3`。
- **`webapp/jobs.py`** 是状态机所在；所有 `JOBS` 字典访问都走 `jobs_put/get/pop/snapshot` helper（`_jobs_lock` 保护），**禁止裸 `JOBS[...]`**。
- **`webapp/ingest.py`** 做三重限额（`MAX_UPLOAD_BYTES` / `MAX_TOTAL_CHARS` / `MAX_SEGMENTS`），并对 `.txt` 做 `utf-8-sig → utf-8 → 400` 编码降级。

## 修改时注意

- 不要把 `tts_core.py` 改成依赖环境变量；CLI 和 webapp 各自构建 `TTSConfig` 后传入。
- 不要"修正" `app.token = "access_token"` 字面量或 `Bearer;` 分号——见顶部"协议铁规"。
- 不要打印整个 `TTSConfig` / `Job`，那会触发实现者自定义 `__repr__` 时漏掉敏感字段。
- 单段失败仅跳过本段、不中断整体流程；保持这种容错语义。
- 修改 `MAX_*`、`SPEED_RATIO`、`LOUDNESS_RATIO`、并发模型相关常量需同步更新本文件。

## PaaS 部署 runbook

通用前提（任一平台）：

1. 仓库准备：`git init` → 提交 → `git push` 到 GitHub。**不要**提交 `.env`（已在 `.gitignore` 里）。
2. 平台后台配 **Environment Variables**：
   - 必填：`VOLCENGINE_APPID` / `VOLCENGINE_ACCESS_TOKEN` / `VOLCENGINE_VOICE_TYPE` / `ACCESS_CODES`
   - 可选覆盖默认：`MAX_CONCURRENT_JOBS`、`RATE_LIMIT_PER_MINUTE`、`JOB_TTL_SECONDS`、`JOB_MAX_RUNTIME_SECONDS`、`MAX_TOTAL_CHARS`、`MAX_UPLOAD_BYTES`、`MAX_SEGMENTS`
3. 平台使用 `Dockerfile` 路径（项目根）。Dockerfile 已显式 `--workers 1`，不要在平台 UI 里改启动命令把它去掉。
4. 健康检查：`GET /health`。
5. **不要开多实例 / 自动扩容**。MVP 的内存 JOBS 模型只在单实例单 worker 下正确。

### Railway

- New Project → Deploy from GitHub repo → 选这个仓库。
- Railway 会自动识别根目录的 `Dockerfile`。
- Settings → Variables 加上述所有 env vars。
- Settings → Networking → Generate Domain。
- Settings → Healthcheck Path = `/health`。
- Settings → Replicas = 1（默认就是 1，确认即可）。

### Render

- New → Web Service → Connect repo。
- Runtime 选 **Docker**（不要选 Native）。
- Dockerfile Path = `Dockerfile`，Docker Context = `./`。
- Instance Type 选最便宜的足够即可（合成是 CPU/IO 型，512MB 内存够用）。
- Environment 加上述 env vars。
- Health Check Path = `/health`。
- Auto-Deploy on push 可开。

### Fly.io

- `fly launch` 在仓库根目录执行；当问到"Dockerfile detected"选 Yes。
- 生成的 `fly.toml` 里检查 `[http_service]` 段：`internal_port = 8000`、加 `[[http_service.checks]] path = "/health"`。
- `fly secrets set VOLCENGINE_APPID=... VOLCENGINE_ACCESS_TOKEN=... VOLCENGINE_VOICE_TYPE=... ACCESS_CODES=...`
- **重要**：`fly scale count 1`，确保只一台机器；若默认开了多区域，用 `fly scale count 1 --region <你最近的> --region-by-app` 收成单实例。
- `fly deploy`。

### 上线后自检清单

```
curl -sf https://<你的域名>/health           # → {"ok":true}
curl -i https://<你的域名>/api/jobs \
  -X POST -F source_type=text -F text=hi      # → 401 (缺 X-Access-Code)
curl -i -H "X-Access-Code: <你的码>" \
  https://<你的域名>/api/jobs \
  -X POST -F source_type=text -F text=测试    # → 200 {job_id, job_token}
```

浏览器打开 `https://<你的域名>/`，输入访问码，提交一段短文本，确认能下载到有效 MP3（已在本地端到端验证为 6 秒、24KB 的合法 MP3）。

## 后续可演进（README 备忘，不在 MVP 范围）

- 内存 JOBS → Redis + RQ（解决重启丢、支持多 worker）
- 临时盘 → S3 / OSS（解决 PaaS 容器重启丢文件）
- 一码一额度（每码每天 N 次 / N 字）
- 前置文本审核（省 API 费）
- Sentry 错误上报
