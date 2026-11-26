# CodeBuddy 账号管理器 · 网页版

桌面版（Windows）的服务器移植：核心模块（cb_api / wb_api / wb_growth /
cb_app / account_store / cb_cloud）原样复用，只替换 Windows 专属层：

| 桌面版 | 网页版 |
|---|---|
| DPAPI 加密账号库 | `cb_secrets.py` 替身：本地随机密钥 AES-256-GCM（store.key，0600） |
| Windows 计划任务 / 开机自启 | `server.py` 内置调度线程：每日 09:05 全量（续期+签到+成长计划）+ 每 N 小时成长计划 |
| tkinter GUI | `server.py` 内置网页（密码登录、账号列表、导入、云端同步、日志、设置） |
| 读本机 IDE 的 product.json 定网关 | 设置页/环境变量 CB_API_ENDPOINT，默认国内版 https://www.codebuddy.cn |

桌面版专属功能（切号写回 IDE、同步到本机 WorkBuddy、从 IDE 导入）在网页版
不可用，替身模块会明确报错；签到/续期/额度/成长计划/云端保险库全部可用。

## 部署（Altare w1 沙盒）

- 代码：`/root/cbam/`，数据：`/root/cbam-data/`（APPDATA 指向它）
- Python venv：`/root/cbam-venv`（只装 cryptography）
- 启动：`/root/cbam/run.sh`（看门狗循环），由沙盒启动钩子拉起，监听沙盒内
  部端口（Altare 端口槽 25471 映射），公网入口 https://owl.ipv64.net
- 首次访问：服务器生成一次性设置令牌（在 web.json / 启动输出里），用它在
  /setup 设置管理密码；之后密码登录（30 天会话）。

## 账号导入

1. 粘贴导入：与桌面版相同的登录态 JSON（可多条）；
2. 云端同步：桌面版先「云端同步 → 上传」，网页版在导入页填同步码拉取合并；
   设置页保存同步码后，可把服务器续期后的最新登录态一键推回云端。

⚠️ 网页版成为签到/续期的唯一执行者后，建议关掉桌面版的计划任务与开机自启，
避免两边同时用 refresh token 续期互相顶号（桌面版 README 的稳妥做法）。

致谢：活跃上报、官网网页会话、积分补偿等接口的协议形状参考了 workbuddy2api-panel 的协议分析（AGPL-3.0）：https://github.com/yreidev/workbuddy2api-panel ，本项目为独立 Python 实现，未复制其代码。
