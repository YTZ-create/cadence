# Security

## 不要提交的内容

- `cmf-watch/tools/.watch_state.json`（设备 secret/authkey）
- `workspace/data/` 下的任何个人健康数据
- `openclaw.json`、`.env`、`config.py` 或任何 API key
- 含真实 open_id、cron ID、内网地址的日志

仓库的 `.gitignore` 已覆盖这些路径；提交前仍建议运行一次人工检查。

## BLE 桥

- 默认只绑定 `127.0.0.1:8765`；
- 同一时间只允许一个 BLE 客户端；
- 不要将桥端口暴露到局域网或公网；
- 不要在日志中输出 authkey、sessionKey 或 secret。

## 漏洞报告

如果发现会导致密钥泄露、任意文件读写或远程代码执行的问题，请通过 GitHub Security Advisory 私下报告，不要在公开 Issue 中贴出利用细节或真实数据。
