# Linux 部署、升级与备份

以下为独立新实例模板，需要 Python 3.12+、Node 22.12+、Nginx 和 systemd。所有文件均从此仓库取得，不连接任何既有 GHOST 服务。

## 安装

1. 创建系统用户 `observetrack`，代码放入 `/opt/observetrack`。不要复用其他应用的用户、环境文件或数据库。
2. 在项目目录创建 `.venv` 并安装 `requirements.txt`；在 `frontend` 执行 `npm ci && npm run build`。代码、虚拟环境和构建文件需要让服务用户可读，代码无需给服务写权限。
3. `install -d -o observetrack -g observetrack -m 700 /var/lib/observetrack`。
4. 从 `.env.example` 创建 `/etc/observetrack.env`，权限 `root:root 600`，配置独立管理员密码、所需 Token/模型，设置 `OBSERVE_DATA=/var/lib/observetrack` 和 `OBSERVE_SECURE_COOKIE=true`。systemd 直接读取此文件，不需要在代码目录放 `.env`。
5. 安装 `deploy/observetrack-api.service`、`deploy/observetrack-worker.service` 到 `/etc/systemd/system/`，执行 `systemctl daemon-reload`、`systemctl enable --now observetrack-api observetrack-worker`。只运行一个 API 进程和一个 worker。
6. 为自己的域名配置 DNS，并取得有效 HTTPS 证书。修改 `deploy/nginx.conf.example` 中的域名和证书路径后安装，先 `nginx -t` 再 reload。配置证书自动续期，并在续期成功后 reload Nginx。
7. 公网仅开放网站需要的 80/443 和你实际使用的 SSH 端口；API 9420 仅监听本机。若前置其他代理，需要结合真实网络配置可信代理，禁止无条件信任公网转发头。

初次启动缺少管理员密码时无法登录；缺少模型配置时仍能生成事实简报。改变管理员密码并重启 API 会注销现有会话。模型地址可访问内网服务，仅允许可信管理员维护环境文件。

## 核对

```sh
systemctl is-active observetrack-api observetrack-worker
curl --fail http://127.0.0.1:9420/health
journalctl -u observetrack-worker --since today
```

通过 HTTPS 登录，确认会话 Cookie 有 Secure 属性、观察列表和报告为空，再添加公开仓库建立基线。不要通过浏览器访问不受信任的开发服务器。

## 升级与回退

停止这两个服务，备份 `/etc/observetrack.env` 和 `/var/lib/observetrack` 整个目录（包括可能存在的 SQLite WAL），并记录当前版本。将已测试的新代码、依赖和前端构建部署到原目录，再启动服务并检查健康、登录和运行记录。

回退时先停止服务，恢复对应旧版代码、依赖、前端及升级前的数据备份，再启动；不要在运行中直接覆盖 SQLite 文件。备份含私有证据、会话和模型记录，应加密保存，禁止放进网站根目录或提交到仓库。

本地新实例不需要数据库迁移。历史 ObserveTrack 数据结构保留，但导入生产数据库属于额外操作；此项目不自动读取或迁移它。
