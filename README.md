# ObserveTrack

独立运行的 GitHub 公开仓库观察工作台：跟踪变化、保存证据，使用可选的兼容 OpenAI Chat Completions 的模型接口生成中文日报和周报。

## 功能

- 最多跟踪 20 个公开仓库，采集提交、Release、Tag、Issue、PR、评论、Discussion 和公开安全公告。
- 首次建立基线，并回补最近 7 天可获取的活动；完整检查无变化时跳过模型，只有指标变化时保留图表。
- 增量游标、不可变证据、报告版本、运行记录与模型用量存入 SQLite；分页失败、限流和缺少权限不会标记为“无变化”。
- 中文日报/周报、证据引用、SVG 图表，以及 Markdown、HTML、ZIP 导出。
- 单管理员登录、CSRF 校验；可选 V2 主编与专家分析及手动继续。
- GitHub 只读：不创建 Issue、PR 或评论。没有通用聊天、沙盒或社区代理。

## 本地安装

需要 Python 3.12+、Node.js 22.12+（推荐 Node 24 LTS）。

```sh
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell 使用：.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env
# Windows 可用 Copy-Item .env.example .env
```

编辑 `.env`，至少填写 `OBSERVE_ADMIN_PASSWORD` 为独立强密码。请勿提交 `.env`。运行时环境变量优先于 `.env`；不读取其他历史凭据文件。

```sh
cd frontend
npm ci
npm run build
cd ..
python -m uvicorn observetrack.app:app --host 127.0.0.1 --port 9420
```

另开终端，使用相同项目目录、虚拟环境和环境配置运行：

```sh
python -m observetrack.worker
```

打开 http://127.0.0.1:9420 ，登录后添加 `owner/repository`。前端开发可运行 `npm run dev`，仅监听本机；开发端口通过代理访问后端 `/api`。

## 配置

| 变量 | 用途 |
| --- | --- |
| `OBSERVE_DATA` | SQLite 和 worker 锁目录，默认 `data`；API 与 worker 必须相同 |
| `OBSERVE_ADMIN_PASSWORD` | 管理密码；修改后重启 API，会注销旧会话 |
| `OBSERVE_GITHUB_TOKEN` | 可选 GitHub Token；公开 REST 可无 Token 调用，Discussions 需授权 |
| `OBSERVE_MODEL_BASE_URL` | 包含 API 版本路径的基础地址，程序追加 `/chat/completions` |
| `OBSERVE_MODEL_NAME` / `OBSERVE_MODEL_API_KEY` | 模型 ID 与密钥；三项缺一时保留事实简报，不调用模型 |
| `OBSERVE_SECURE_COOKIE` | 本机 HTTP 用 `false`；公网 HTTPS 必须设 `true` |
| `OBSERVE_ANALYSIS_V2` | 可选 V2 分析，默认 `false` |
| `OBSERVE_ANALYSIS_V2_REPOSITORIES` | 可选仓库内部 ID 列表，逗号分隔；空值表示全部 |

模型服务商和 GitHub 可能收费或限流。报告会将选中的公开内容、代码差异和证据发送到你配置的模型服务。请自行评估供应商的数据政策。预算是调用上限，不保证固定金额；V2 可能有多次主编/专家调用。

默认北京时间 07:30 采集，08:00 汇总前一天，周一 08:30 生成上周周报；界面可以调整时间与预算。首次手动检查不必等待日程。模型分析被中断后不自动重放，需在运行记录中明确重试或继续。

## 限制

每个仓库最多同时执行一个任务，全局最多两个任务；Linux 使用文件锁阻止重复 worker。Windows 仅支持启动一个 worker。每轮 GitHub 最多 350 个请求、单分页最多 100 页；大型仓库可能采集不全。缺失数据明确保留为不完整，不代表没有活动或安全问题。

图表只用真实采样，不恢复历史星标、已删除内容或不可见讨论。模型总结可能出错，应核对引用。默认保留 365 天，可在界面降低保留时间。

## 部署与开发

- [Linux HTTPS 部署与备份](docs/DEPLOYMENT.md)
- [开源准备与验证记录](docs/RELEASE_READINESS.md)
- [来源](PROVENANCE.md) / [第三方声明](THIRD_PARTY_NOTICES.md) / [安全问题](SECURITY.md)

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
cd frontend
npm test
npm run build
```

Apache-2.0，Copyright 2026 Kisara-Lab。前端 `private: true` 仅防止误发布到 npm，不限制按许可证使用代码。
