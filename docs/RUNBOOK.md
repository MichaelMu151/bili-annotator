# 让标注服务常驻（launchd）——手机随时能连

## 为什么需要这个
之前服务是"临时进程"，会话一结束/电脑睡眠/换网，手机就打不开（`连接被拒绝`）。
现在用 macOS `launchd` 把服务做成**用户级常驻服务**：登录即启动、崩溃自动重启、
并 `caffeinate -s` 防止电脑睡眠断网。

> ⚠️ 项目**必须**放在家目录（`~/bili-annotator`），**不能放 `~/Desktop`**：
> macOS 的 TCC 隐私保护禁止后台 launchd 服务读取 `Desktop`，会报
> `Operation not permitted`。本仓库已位于 `~/bili-annotator`。

## 当前部署（已安装）
- 服务标签：`com.mushiquan.biliannotator`
- 包装脚本：`scripts/serve.sh`（绑定 `0.0.0.0:5005` + caffeinate）
- plist：`scripts/com.mushiquan.biliannotator.plist`
- 已复制到：`~/Library/LaunchAgents/com.mushiquan.biliannotator.plist`
- 专用 venv：`~/.venvs/biliannotator`（在 Desktop 之外，避免 TCC 限制）

## 常用命令
```bash
# 查看是否在跑
launchctl list | grep biliannotator

# 启动 / 停止 / 重启
launchctl load   -w ~/Library/LaunchAgents/com.mushiquan.biliannotator.plist
launchctl unload    ~/Library/LaunchAgents/com.mushiquan.biliannotator.plist
launchctl kickstart -k gui/$(id -u)/com.mushiquan.biliannotator

# 看日志
tail -f ~/bili-annotator/data/server.out.log
tail -f ~/bili-annotator/data/server.err.log

# 本机/局域网自检
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5005/
curl -s -o /dev/null -w "%{http_code}\n" http://<你的LAN_IP>:5005/
```

## 手机访问
- 同一 Wi-Fi 下打开：`http://<电脑LAN_IP>:5005`（用 `ipconfig getifaddr en0` 查 IP）
- 或固定地址（IP 变了也能用）：`http://mushiquandeMacBook-Pro-3.local:5005`

## 注意
- 改了 `app.py` 后需重启服务才能生效：`launchctl kickstart -k gui/$(id -u)/com.mushiquan.biliannotator`
- 电脑**关机**时服务不可用（睡眠不会，已 caffeinate）；需要 7x24 请用常开设备或部署到服务器。
- 这是 Flask 开发服务器，仅适合本地/小范围 pilot；多人正式使用请换 waitress/gunicorn。
