# 隐私与数据边界

## 仓库里有什么

- 只包含通用代码、算法、合成示例和文档；
- 不包含真实睡眠、心率、血氧、压力、反应力、自评、日程或聊天数据；
- 不包含设备 authkey、BLE secret、API key、飞书 open_id、cron ID 或本机绝对路径。

## 本地会生成什么

运行同步或分析后，数据只写本机：

```text
workspace/data/health/watch/     # 原始归档
workspace/data/sleep/            # 睡眠账本
workspace/data/health/energy/    # 自评、问卷、事件、模型
workspace/data/health/reaction/  # 反应力测试
workspace/data/health/recovery/  # 恢复分
cmf-watch/data/                  # 桥接缓存、日志、同步状态
```

这些目录已被 `.gitignore` 排除。

## 设备密钥

- `cmf-watch/tools/.watch_state.json` 保存设备 `secret` 和 `authkey`；
- 该文件只留在本机，不提交、不打印、不外发；
- 公开版本的第三方工具已把 authkey/sessionKey 调试输出改为 `<redacted>`；
- 更换设备或怀疑泄露时，删除该文件并重新配对。

## 网络行为

- BLE 桥只监听 `127.0.0.1:8765`，不对外开放；
- 同步和分析不需要云端；
- 可选 OpenClaw/飞书通知使用环境变量配置，未配置时不发送；
- 若启用通知，只发送摘要文本，不发送原始健康数据或密钥。

## 删除数据

停止同步后直接删除上述本地数据目录即可。仓库本身不保存个人数据。

## 注意

恢复分、精力分和高能时段是个人参考，不是医学诊断，也不应作为就医或用药依据。
