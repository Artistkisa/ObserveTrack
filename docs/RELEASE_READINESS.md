# 本地开源准备记录

检查日期：2026-11-13。授权主体及版权署名：Kisara-Lab；项目许可证：Apache-2.0。

## 版本边界

本目录是新建的独立 Git 仓库，未继承任何历史提交。只复制采集、报告、分析、前端及相关测试源码；原项目和生产部署未改动。

社区代理页面、代理 API、配置、专用测试及遗留表定义已移除。ObserveTrack 的数据结构和 API 保留。公开版使用新的空数据目录，不包含账号、聊天、证据数据库、Token、登录凭据、生产域名/IP、历史压缩包或验收截图。

保留用户已授权的 GHOST 代码适配及幽灵 SVG。第三方依赖的许可证已按元数据整理，并汇总本机所安装包的许可证原文；MPL-2.0 的 certifi 和 lightningcss 相关义务在第三方声明中明确列出。

## 已执行验证

| 检查 | 结果 |
| --- | --- |
| Windows / Python 3.14 后端单元与集成测试 | 43 项通过 |
| Node 24 前端测试 | 2 项通过 |
| Vite 8.3.8 生产构建 | 通过 |
| 独立真实 API 与 worker 进程 | 健康检查、心跳、静态页面、登录及空列表通过 |
| 模拟 GitHub → 变化 → 模拟模型 → 引用报告 | 通过 |
| 完整采集无变化 → 不调用模型 | 通过 |
| 分页失败保留游标、不误报无变化 | 通过 |
| 中断不重放模型、保留分析成果 | 通过 |
| HTTPS 模式会话 Secure Cookie | 通过 |
| 移除的页面与代理接口 | 返回 404 |
| pip check | 依赖兼容性通过 |
| pip-audit：锁定运行依赖 | 未发现已知漏洞 |
| npm audit：包括开发依赖 | 未发现已知漏洞 |
| 待提交文件及最终源码 ZIP 模式扫描 | 未发现秘密格式、生产地址、私人路径或运行数据 |

修复了调度测试依赖当前日期的问题，以及 V2 测试隐含依赖本机模型配置的问题。测试使用显式虚构配置和模拟传输，不调用真实 GitHub 或付费模型。

依赖检查发现并更新了受影响的旧 Vite、Starlette 和 python-dotenv；更新后的版本和兼容组件已锁定并重新测试。

## 检查范围与限制

此轮没有部署 Linux 服务或访问生产服务器，没有使用真实 GitHub/模型做端到端请求，也没有进行浏览器自动化视觉验收。Linux HTTPS/systemd 文件是通用部署模板。已配置 Python 3.12/3.14 和 Node 24 的 CI，但尚未在远程 CI 执行。

Starlette 在使用 httpx 的测试客户端时会输出弃用提示，目前兼容且测试通过；未来升级测试客户端时需重新验证。

漏洞库结果只代表检查时可用的数据，不代表全面安全审计。模式扫描也不能证明绝无秘密；发布前仍需审阅新增文件。公开仓库暂未创建，未上传代码、未打 tag 或发布线上版本。

## 复现与导出

按 README 安装运行与开发依赖，并先构建前端。

```sh
python -m unittest discover -s tests -v
python scripts/smoke_local.py
python scripts/check_public_source.py
python -m pip_audit -r requirements.txt
cd frontend
npm test
npm run build
npm audit
cd ..
git archive --format=zip -o artifacts/ObserveTrack-source.zip HEAD
python scripts/check_public_source.py --zip artifacts/ObserveTrack-source.zip
```

导出前创建被忽略的 `artifacts` 目录。导出只使用已提交文件，不将本机 `.venv`、`node_modules`、构建产物或运行数据收入源码 ZIP。分发前端构建时另行附带 LICENSE、NOTICE 和第三方声明。
