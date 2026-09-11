# CodeBuddy 账号管理器（Windows）

> **作者：这是哪头猪？**
> 仓库：https://github.com/zhuzhuxia-star/codebuddy-account-manager
> 版本：1.1.0

管理你自己的多个 CodeBuddy 账号登录态，一键把选中账号"切"到 CodeBuddy IDE 使用。

## 下载

**➡️ [点这里下载最新版 CodeBuddyAccountManager.exe](https://github.com/zhuzhuxia-star/codebuddy-account-manager/releases/latest)**

单文件、免安装（Windows 10/11）。用法：

1. 把 `CodeBuddyAccountManager.exe` 放到 `%APPDATA%\CodeBuddyAccountManager\bin\`
   （目录不存在就新建；**从 `dist` 等其它路径启动在部分机器上会被安全软件拦住**）
2. 双击运行
3. 首次使用点「从当前 IDE 导入」或「粘贴导入登录态」把账号加进来

> 全部历史版本见 [Releases](https://github.com/zhuzhuxia-star/codebuddy-account-manager/releases)。

## 原理

CodeBuddy（VS Code 系）把扩展登录态加密存放在其**用户数据目录**（本机为国内版
`%APPDATA%\CodeBuddy CN\`，旧版可能是 `%APPDATA%\CodeBuddy\`）：

```
<数据目录>\Local State                      <- 主密钥 (os_crypt.encrypted_key, DPAPI)
<数据目录>\User\globalStorage\state.vscdb   <- 密文 (SQLite ItemTable)
```

工具启动时**自动检测**数据目录（优先较新的、含 Local State + state.vscdb 的目录）。
登录态条目 key 名因版本而异（旧版 `planning-genie.new.accessToken`、国内版
`...accessTokencn`），工具按"key 名含 accessToken/refreshToken 或能解出登录态 JSON"
识别账号条目，切换时只替换登录态条目、保留缓存/草稿等其它条目。

本工具在同一台机器上用 **DPAPI + AES-256-GCM**（与 CodeBuddy 相同算法）把选中账号的
登录态加密写回 `state.vscdb`，替换原账号，实现切号。

- 只适用于**本机、当前 Windows 用户**（DPAPI 绑定用户）
- 切号前**自动备份**原登录态（`state.vscdb.bak-<时间戳>`）
- 账号库存在 `%APPDATA%\CodeBuddyAccountManager\accounts.bin`，同样用 DPAPI 加密

## 使用

> **从哪里启动**：实际运行的是部署副本
> `%APPDATA%\CodeBuddyAccountManager\bin\CodeBuddyAccountManager.exe`（计划任务/开机自启也指向它）。
> `dist\CodeBuddyAccountManager.exe` 是构建产物，仅供分发；部分机器上该路径会被安全软件/索引
> 持续占用，导致双击后弹「Error」窗口而无法启动，所以 `build.bat` 打包后会自动把副本部署到
> `%APPDATA%` 下。开发时也可直接 `python main.py`。

1. 运行 `%APPDATA%\CodeBuddyAccountManager\bin\CodeBuddyAccountManager.exe`（或 `python main.py`）。
2. **添加账号**（三选一）：
   - 「登录新账号」：走 CodeBuddy 官方登录流程（工具请求登录地址 → 自动打开浏览器 →
     你在网页完成登录 → 工具自动取回 token 与账号信息保存）。保存的登录态可直接用于切号；
   - 「从当前 IDE 导入账号」：把 CodeBuddy 现在登录的账号收进工具列表；
   - 「粘贴导入登录态」：粘贴 CodeBuddy 账号登录态 JSON。支持多种形态与一次多条：
     - 扩展账号记录视图（顶层含 `auth_raw` / `profile_raw`）；
     - 扩展存储明文（顶层含 `account` + `auth`）；
     - 松散 token 对象（`access_token` / `refresh_token`）；
     - 对象或数组、一段或多段 JSON 均可一起粘贴（会自动逐段识别）。
     ⚠️ 请粘贴**完整未脱敏**的 JSON，否则 token 无效、切号不生效。
3. 选中账号 → 「切换到选中账号」。
   - 可 **Ctrl/Shift 多选或点「全选」** 后统一操作：刷新额度 / 续期登录态 / 删除选中，
     批量结束后会弹出明细（每行 OK/失败原因）。
   - 若 CodeBuddy 正在运行：先手动退出，或勾选"自动结束并重新打开"（注意：会强制结束
     CodeBuddy，未保存内容会丢失）。
   - 切换完成后重新打开 CodeBuddy，即为目标账号。切号会**同时覆盖新旧两套登录条目**
     key（`accessToken` / `accessTokencn`），避免版本不匹配导致"切了没生效"，写回后
     工具会重新解密校验并告知当前实际读到的账号。
4. 「刷新额度」：联网按 **IDE 同款统计口径**（`CycleCapacity*Precise` 周期数值、
   有效资源包过滤、主套餐判定）查询所选账号，列表"套餐/额度"两列会更新。
   - 额度格式：`剩余/总量（已用 x）· 日期`；免费/试用账号显示**额度周期刷新日**，
     专业订阅显示扣费到期日。token 失效时自动用 refresh token 续期后重试。
5. 「续期登录态」：用 refresh token 换取新的 access token 并更新账号库（避免过期）。
6. 「同步到 WorkBuddy」：把选中账号的登录态写入 WorkBuddy 使用的登录库。
   - WorkBuddy 是 CodeBuddy 的**伴侣壳**（见 `%APPDATA%\WorkDaddy\workbuddy-target.json`
     的 `binary` / `dataRoot`）：它不单独存一份账号，而是复用其拉起的 CodeBuddy 实例的
     登录态，所以同步用的仍是同一套 DPAPI + AES-256-GCM 算法。
   - 目标目录按 `dataRoot` → `%APPDATA%\WorkDaddy` → `%APPDATA%\CodeBuddy CN` 顺序自动
     探测，底栏会显示实际写入的目录；「切换到选中账号」时默认会一并同步。
   - 同步前请先退出 CodeBuddy / WorkBuddy（`state.vscdb` 被占用时无法写入）。
7. 「立即签到」/「查询签到状态」：调用 WorkBuddy 的每日积分签到接口（与客户端一致，
   走 CodeBuddy 同一网关）：
   - 签到：`POST /v2/billing/meter/daily-checkin`
   - 状态：`POST /v2/billing/meter/checkin-activity-status`
   - 结果写回账号库，列表「签到」列显示今日是否已领与获得积分；**重复调用是安全的**——
     服务端返回 `10001`（今天已签到）按"今日已签到"处理，不计为失败。
8. 「每日定时（续期+签到）」：把 `checkin-all` 注册为 Windows 计划任务（默认每天 09:05）。
   **每次运行会先给全部账号续期登录态（refresh token 换新 access token 并写回账号库），
   再逐个调用每日签到**，续期失败不影响签到（旧 token 有效照样能签）。
   按钮文字显示当前是否已开启；每次结果追加到
   `%APPDATA%\CodeBuddyAccountManager\checkin.log`。
   想只续期不签到：`python main.py refresh-all`。
9. 「云端同步…」：把整份账号库**加密后**存到 `https://md.dcio.eu.org/` 的一篇文章里，
   实现**跨电脑保存账号**。新电脑装好后点「从云端同步」即可取回全部账号（含登录态）。
   - **不需要自己编口令**：直接点「生成同步码」，工具会生成一串随机码并复制到剪贴板；
     平时本机已记住，只有**换电脑时**需要把它粘到新机器的同一个框里。
   - 账号数据用 `PBKDF2-SHA256(同步码, 20 万次)` 派生密钥做 AES-256-GCM 加密 + zlib 压缩，
     文章正文里只有 base64 密文。推送后工具会**立即回读校验**，解密不一致会明确报错。
   - ⚠️ 那个文章是**公开可访问**的（页面还允许搜索引擎收录），所以**绝不能放明文 token**：
     等于把账号通行证公开出去。安全完全依赖同步码强度，同步码丢了数据也找不回来。
   - 首次点「上传」创建文章（标题固定 `CBAC-VAULT-v1`），之后点「上传」更新同一篇；
     换电脑可先粘贴云端文章 URL/slug 再同步。
   - 本机记住的同步码用 DPAPI 加密存放，仅供无人值守的一键同步使用。
10. 「开机自启（续期+签到）」：登录后自动执行一次 `checkin-all`（同样**先续期再签到**），
    写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，**不需要管理员权限**。
    与「每日定时（续期+签到）」可同时开启，互为兜底；两者都是幂等的。
    > 多台电脑同时开自动续期时，若服务端对 refresh token 做一次性轮换，可能出现互相顶号；
    > 实测本服务端可重复使用，风险较低。稳妥做法是只在一台机器开自动续期，再靠云端同步
    > 把最新登录态带到其它机器。

### 导入/登录时做了什么

无论来源是粘贴、IDE 导入还是软件登录，保存前都会统一转换成 CodeBuddy 实际读取的
**会话明文**结构（缺一不可，否则 IDE 不认）：

```json
{
  "id": "Tencent-Cloud.genie-ide-cn",   // 对齐当前 IDE 的客户端标识
  "domain": "www.codebuddy.cn",
  "converted": true,
  "account": {"id": "<uid>", "uid": "<uid>", "label": "...", "nickname": "...", ...},
  "auth":    {"accessToken": "<jwt>", "refreshToken": "<jwt>", "expiresIn": ...},
  "accessToken": "<uid>+<jwt>",
  "refreshToken": "<jwt>",
  "token": "<jwt>",
  "expiresAt": 1793600000000
}
```

## 验证解密/切号是否成功

- 工具底栏会显示当前 IDE 生效账号名；
- 命令行：`python main.py ide-account`。

## 命令行

| 命令 | 说明 |
|---|---|
| `python main.py list` | 列出账号 |
| `python main.py ide-account` | 显示当前 IDE 生效账号 |
| `python main.py checkin-all` | 全部账号：**先续期登录态再签到**（计划任务调用的就是它；`--no-refresh` 可只签到） |
| `python main.py refresh-all` | 只续期全部账号的登录态 |
| `python main.py checkin-status` | 只查询签到状态，不领取 |
| `python main.py wb-status` | 显示 WorkBuddy 目标目录与当前账号 |
| `python main.py wb-sync` | 把当前 IDE 登录态同步到 WorkBuddy |
| `python main.py cloud-status` | 云端账号库状态（地址、账号数、更新时间） |
| `python main.py cloud-push [口令]` | 账号库加密上传（口令可省略，用本机记住的） |
| `python main.py cloud-pull [口令] [slug/URL]` | 从云端取回并合并到本地 |
| `python main.py install-task 09:05` | 注册/更新每日定时签到 |
| `python main.py uninstall-task` | 取消每日定时签到 |
| `python main.py install-autostart [分钟]` | 设置开机（登录）后自动签到 |
| `python main.py uninstall-autostart` | 取消开机自启签到 |

## 开发 / 打包

```bat
pip install -r requirements.txt
python main.py              # 运行 GUI
build.bat                   # 打包为 dist\CodeBuddyAccountManager.exe
```

> 打包/运行 GUI 需要 Python 自带 Tcl/Tk。若 Python 安装在 Windows 应用商店包
> （WindowsApps）下，Tcl 库目录在 `<安装目录>\tcl\tcl8.6` 且默认找不到，需要在跑
> `python`/PyInstaller 前设置 `TCL_LIBRARY=<安装目录>\tcl\tcl8.6` 和
> `TK_LIBRARY=<安装目录>\tcl\tk8.6`（PyInstaller 靠这两个变量把 tcl/tk 打进 exe）。
> `build.bat` 已自动探测并设置这两个变量。
>
> ⚠️ **开发注意（Store 版 Python 的坑）**：从 Microsoft Store 安装的 Python 带 MSIX
> 包身份，**新创建的文件会被重定向**到
> `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.13_*\LocalCache\Roaming\...`。
> 于是用 `python main.py`（源码）看数据目录，和用户双击 exe 看到的数据目录**不是同一份** ——
> 表现为"exe 读不到源码刚写的配置"。**验证功能请用 exe 跑**，或改用非 Store 版 Python。

核心模块：

| 文件 | 作用 |
|---|---|
| `cb_secrets.py` | DPAPI 主密钥、v10(AES-256-GCM) 加解密、state.vscdb 读写 |
| `account_store.py` | 账号库存取（DPAPI 加密）+ 登录态解析 / 统一转换为 IDE 会话明文 / 额度展示 |
| `cb_api.py` | 服务端接口客户端：官方登录流程、账号列表、额度查询、token 续期 |
| `cb_runtime.py` | CodeBuddy 进程检测 / 启动 |
| `cb_app.py` | 业务流程：从 IDE 导入、切号写回、软件内登录、额度、续期、签到 |
| `wb_api.py` | WorkBuddy 签到接口：活动状态查询 / 每日签到 + 业务码→状态映射 |
| `wb_target.py` | WorkBuddy 目标定位（target 配置 + 数据目录探测）与登录态同步 |
| `cb_cloud.py` | 云端保险库：口令派生密钥 + AES-256-GCM 加密，publish/update 到 md.dcio.eu.org |
| `cb_task.py` | 签到自动化：每日计划任务 + HKCU Run 开机自启 + 签到日志 |
| `cb_log.py` | 统一日志（接口请求/批量任务/签到，落盘 + 界面可查） |
| `gui.py` | tkinter 图形界面 |

接口与内置插件一致（`/v2/plugin/auth/state`、`auth/token`、`login/account`、
`accounts`、`auth/token/refresh`、`/v2/billing/meter/get-payment-type`、
`get-user-resource`），服务地址取自 IDE 安装目录 `genie/product.json` 的 `endpoint`，
可用环境变量 `CB_API_ENDPOINT` 覆盖。网关会拦截默认 UA，请求已自带 `curl/8.0.1` UA。

## 注意事项

- 仅供管理**你自己有权使用**的账号；本工具只在你本机读写 CodeBuddy 的登录态文件。
- 请勿把登录态 JSON / 备份文件外发给他人（等价于交出账号通行证）。
- CodeBuddy 升级若改变加密格式/存储结构，工具可能失效，需相应适配。
- `state.vscdb` 被 CodeBuddy 占用时 SQLite 可能被锁，因此切号要求先退出 CodeBuddy
  （或勾选自动结束）。
- 签到接口请只用于**你自己的账号**，且保持低频（每天一次即可）；工具本身做了幂等处理，
  重复调用不会重复领取。
- 计划任务执行的是"当前这份代码"（源码运行用 `pythonw main.py`，打包后用 exe），
  移动项目目录后需重新执行 `install-task` 刷新命令。
- **云端同步的安全边界**：工具只在**加密后**上传，文章里永远不含明文 token；但文章链接
  是公开的，请把同步口令当作账号密码保管。若怀疑口令泄露，重新设置口令并再点一次「上传」
  即可用新密文覆盖旧文章。
- 排障：所有接口请求、批量任务与签到结果都会写到
  `%APPDATA%\CodeBuddyAccountManager\app.log`。**主窗口下方就是实时日志面板**，不需要另开窗口：
  打开界面会自动载入最近的历史日志，之后新日志实时追加；「折叠日志/展开日志」收起或展开，
  「清空显示」只清界面不动文件，「打开日志文件夹」直接定位到日志文件。
