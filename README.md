# GameHub

GameHub 是基于 [Hical](https://github.com/Hical61/Hical)、C++20 协程和 MySQL 的多游戏服务端中台，负责渠道回调、到账通知、公告和配置分发。商品、活动和发奖逻辑由游戏服处理。

项目仍在开发中，尚未正式发布。内置 `demo-hmac-v1` 演示协议，真实支付渠道和游戏发奖由使用方接入。

## 功能

- 多游戏、多渠道接入，验签后将事件、逐服任务和审计记录一起提交，按业务身份持久化去重。
- 异步投递、退避重试、租约回收、持久尝试历史和人工重排，支持按游戏及区服分配并发额度。
- 配置版本分发、JWT 管理权限、游戏范围隔离和独立回调限流。
- 业务身份查单、任务筛选、共享暂停及配额控制、逐服配置应用回报。
- 任务归档、全局及游戏级统计、数据库及投递耗时指标、密钥轮换和维护工具。

通知可能重复投递，游戏服须在业务事务中持久化去重，提交后再确认。

[架构说明](docs/architecture.md) · [接入自己的项目](docs/integration.md) · [接口契约](docs/api.md) · [可运行的独立宿主](examples/custom-host/README.md)

公开源码包含完整的通用服务、演示、迁移、测试和接入示例。根目录 `private/` 集中保存使用方的真实接入及本地资料，已被忽略且不参与公开构建、测试和安装；拉取公开源码不需要维护者的本地游戏项目。

## 构建

以下使用 Windows、Visual Studio 2022 和 PowerShell 7。需安装 CMake 3.28+、Git、Python 3.10+；Boost 1.90+、OpenSSL 和 zlib 由 vcpkg 安装。

```powershell
git clone https://github.com/Hical61/GameHub.git
Set-Location GameHub
git clone --filter=blob:none --no-checkout https://github.com/Hical61/Hical.git _deps/hical
git -C _deps/hical checkout --detach 5ba57c95575401bf36499094fcf42eb900707939
git clone --filter=blob:none --no-checkout https://github.com/microsoft/vcpkg.git _deps/vcpkg
git -C _deps/vcpkg checkout --detach 301856f5f2824f788a3ffa6332293861cccd23b4
& ./_deps/vcpkg/bootstrap-vcpkg.bat -disableMetrics
cmake -S . -B build-msvc -G "Visual Studio 17 2022" -A x64 `
  "-DCMAKE_TOOLCHAIN_FILE=$PWD/_deps/vcpkg/scripts/buildsystems/vcpkg.cmake" `
  -DVCPKG_TARGET_TRIPLET=x64-windows "-DGAMEHUB_HICAL_SOURCE=$PWD/_deps/hical"
cmake --build build-msvc --config RelWithDebInfo --parallel 4
ctest --test-dir build-msvc -C RelWithDebInfo --output-on-failure
```

Hical 使用上述固定提交，CMake 会在构建副本中应用 [HTTP 客户端补丁](cmake/patches/hical-response-body-limit.patch)。安装命令：`cmake --install build-msvc --config RelWithDebInfo --prefix stage`，程序和 DLL 位于 `stage/bin/`。

## 测试

准备 MySQL 8.0.16+ 的 `mysqld` 和 `mysql`，替换下面的工具目录：

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -m unittest discover -s tests -p "test_*.py"
python -u tests/integration.py --binary build-msvc/RelWithDebInfo/gamehub.exe `
  --mysql-bin "C:/Program Files/MySQL/MySQL Server 9.0/bin"
```

默认跑基础故障和多游戏隔离两组测试，各用独立临时实例，不连接已有数据库。可加 `--suite core` 或 `--suite multi-game` 单独运行，整轮默认限时 600 秒。存储测试程序须与服务程序同目录；失败诊断保存在 `.local/`。

## 运行

使用独立的 Hub 数据库，最低 MySQL 8.0.16。首次部署先由 DBA 创建空库和两个独立账号。下面两个密码占位符必须替换为各自的随机密码；示例账号仅用于本机连接，远程部署时按实际来源主机调整账号范围。

```sql
CREATE DATABASE gamehub CHARACTER SET utf8mb4;
CREATE USER 'gamehub_migrator'@'localhost' IDENTIFIED BY '<替换为独立随机迁移密码>';
CREATE USER 'gamehub'@'localhost' IDENTIFIED BY '<替换为独立随机运行密码>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES, TRIGGER
    ON gamehub.* TO 'gamehub_migrator'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON gamehub.* TO 'gamehub'@'localhost';
```

迁移账号也是触发器的 DEFINER，迁移后必须保留该账号及本库的 DML（SELECT、INSERT、UPDATE、DELETE）、TRIGGER 权限，供触发器正常执行。可以收回 CREATE、ALTER、INDEX、DROP、REFERENCES 等 DDL 权限，下次迁移前再补回。运行账号只使用上述四项 DML 权限，不授予全局管理权限。

迁移前由 DBA 先做只读检查：

```sql
SELECT @@log_bin, @@log_bin_trust_function_creators;
```

下面的迁移账号命令要求 binlog 已关闭，或 `log_bin_trust_function_creators=1`。如果 binlog 开启且该值为 0，由 DBA / 受控管理账号执行创建触发器的迁移，并确认实际 DEFINER 账号长期保有所需权限；也可按组织策略在维护窗口临时开启 trust，记下原值并在迁移结束时恢复，失败时也要恢复。不要给运行账号扩大权限，也不要让部署脚本自动修改线上全局设置。

新建空库 `gamehub` 后依次执行 [001](migrations/001_initial.sql)、[002](migrations/002_operations.sql)。已有数据库先停止全部 Hub 及其他写入并备份，再确认是否有待执行阶段；需要迁移时只执行与当前程序配套的 002，不重跑 001 或已完成的阶段。

迁移文件固定为 001 和 002，分别用于基础初始化和累积结构迁移。002 根据 `schema_version` 执行尚未完成的阶段。

`schema_version` 记录数据库结构版本，与软件版本独立；程序会校验数据库结构是否匹配，业务代码变更不一定需要迁移。

在仓库根目录执行，按实际环境修改账号和地址。以下数组用于新建空库；已有库需要迁移时改为 `@("migrations/002_operations.sql")`：

```powershell
$migrationFiles = @("migrations/001_initial.sql", "migrations/002_operations.sql")
foreach ($migrationFile in $migrationFiles) {
    Get-Content -LiteralPath $migrationFile -Raw -Encoding utf8 -ErrorAction Stop |
        mysql --default-character-set=utf8mb4 -h127.0.0.1 -P3306 -ugamehub_migrator -p gamehub
    if ($LASTEXITCODE -ne 0) { throw "迁移失败，先检查数据库对象，不要直接重跑。" }
}
mysql --default-character-set=utf8mb4 -h127.0.0.1 -P3306 -ugamehub_migrator -p gamehub `
    --execute "SELECT MAX(version) FROM schema_version;"
if ($LASTEXITCODE -ne 0) { throw "版本检查失败，先不要启动 Hub。" }
```

最后的查询显示已记录的结构版本，启动后的就绪检查会验证它是否与当前程序匹配。迁移必须使用首错停止的非交互客户端，不加 `--force`，不用交互式 `SOURCE`；DDL 失败不能整体回滚，须先检查已执行步骤和数据库对象，不要盲目重跑。

将 [配置示例](config/gamehub.example.json) 复制到被忽略的本地目录，填写数据库和游戏服内网地址。以下联调步骤都在仓库根目录的同一个 PowerShell 终端执行：先设置实际运行账号密码及三个独立随机密钥，将管理令牌写入本地文件，再启动继承当前环境的模拟游戏服，最后启动 Hub。

```powershell
New-Item -ItemType Directory -Path private/config -Force | Out-Null
Copy-Item config/gamehub.example.json private/config/gamehub.json
$credential = Get-Credential -UserName gamehub -Message "输入 Hub 数据库账号密码"
$env:GAMEHUB_HUBDB_PASSWORD = $credential.GetNetworkCredential().Password
$env:GAMEHUB_ADMIN_JWT_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_DEMO_CALLBACK_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_DEMO_OUTBOUND_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
./build-msvc/RelWithDebInfo/gamehub.exe --config private/config/gamehub.json --check-config
if ($LASTEXITCODE -ne 0) { throw "配置检查失败。" }
$adminToken = python scripts/admin_token.py --subject local-operator --role operator --game demo
if ($LASTEXITCODE -ne 0) { throw "管理令牌签发失败。" }
Set-Content -LiteralPath private/config/admin-token.txt -Value $adminToken -Encoding utf8NoBOM -NoNewline -ErrorAction Stop
Remove-Variable adminToken
$receiverProcess = Start-Process -FilePath (Get-Command python -ErrorAction Stop).Source `
    -ArgumentList @('examples/demo_receiver.py', '--port', '9001') `
    -WorkingDirectory $PWD.Path -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $PWD.Path 'private/config/demo-receiver.stdout.log') `
    -RedirectStandardError (Join-Path $PWD.Path 'private/config/demo-receiver.stderr.log') -PassThru
try {
    ./build-msvc/RelWithDebInfo/gamehub.exe --config private/config/gamehub.json
    if ($LASTEXITCODE -ne 0) { throw "Hub 运行失败，请检查本地日志。" }
} finally {
    if (-not $receiverProcess.HasExited) { Stop-Process -InputObject $receiverProcess }
}
```

`--check-config` 只检查配置和环境变量，不连接数据库。启动后可通过 `/admin/ready` 检查数据库是否就绪。

模拟游戏服与 Hub 共用同一 `GAMEHUB_DEMO_OUTBOUND_SECRET`，退出 Hub 后只清理上面保存的接收端进程。新开的并列终端不会继承这些环境变量；如需在新终端启动或重启服务，先从自己的安全配置恢复相同密钥，不要再次随机生成。管理令牌已写入 `private/config/admin-token.txt`，不要输出或提交该文件；在另一个终端切换到仓库根目录后，可直接用令牌文件查控制状态：

```powershell
python scripts/hub_admin.py --url http://127.0.0.1:8443 --token-file private/config/admin-token.txt control-get --game demo
```

## 接入与部署

- 扩展 `games`、`channels`、`servers` 数组接入多游戏、多商户和多区服。`worker.games` 指定本进程负责的游戏，配额只在当前进程生效。
- `ChannelAdapter` 处理渠道协议，`GameAdapter` 处理游戏通信。实际项目适配和游戏数据库访问在私有集成中维护，通过公共接口登记。
- [独立宿主示例](examples/custom-host/README.md)可直接构建，也可复制到自己的工程，链接 `GameHub::service`、`GameHub::bootstrap` 并注入注册表。嵌入时默认关闭公共应用、测试和安装规则。
- 公网代理只开放回调，管理和配置接口放内网。出向 HTTP 仅用于可信内网，跨不可信网络需加 TLS / mTLS。
- [出向 mTLS 代理示例](deploy/nginx-outbound-mtls.conf.example)保留原签名路径并校验目标证书；实际证书、私钥和域名由部署环境提供。
- 密钥、令牌、本地配置和玩家数据不要提交到仓库。

## 公开与私有目录

| 目录 | 内容与提交范围 |
| --- | --- |
| src、cmake、migrations | 通用框架、公开构建及 Hub 自有数据库迁移 |
| examples、config、tests | 演示与独立宿主、合成配置和公共测试 |
| docs、scripts、deploy | 公共接入文档、通用工具和部署示例 |
| private | 本地真实适配、内部资料、实际配置及备份，整个目录禁止发布 |
| .local、build*、_deps | 临时验证、构建及公开依赖产物，保持忽略 |

公开项目不自动发现私有适配器，也不把使用方源码或专用二进制装入默认发行包。发布前运行 `python scripts/check-release.py --all-files` 和 `python scripts/check-history.py`。`python scripts/check-public-source.py` 可导出不含私有内容的完整公开源码副本，用于独立构建和接入验证；检查不会提交或推送。

## 查单与运行控制

管理读取允许对应游戏的 viewer/operator，写入需要 operator。令牌从仓库外文件或 `GAMEHUB_ADMIN_TOKEN` 读取：

```powershell
python scripts/hub_admin.py --url http://127.0.0.1:8443 --token-file C:/secure/hub-token.txt lookup `
  --game demo --task-type recharge_notify --namespace demo --biz-key synthetic-order
python scripts/hub_admin.py --url http://127.0.0.1:8443 control-get --game demo
```

| 接口 | 行为 |
| --- | --- |
| GET /admin/events/{game}/{type}/{namespace}/{key} | 按完整身份查事件及活动、归档任务，不返回玩家载荷 |
| GET /admin/attempts/{game}/{id}/{after} | 分页查询各代次尝试，重排不会清除历史 |
| GET /admin/tasks/{game}/{after} | 支持 status、serverId、createdFrom、createdBefore、limit 查询参数；时间为 UTC Unix 秒 |
| GET /admin/control/{game} | 查看共享控制版本，无覆盖时返回启动配置 |
| POST /admin/control/{game} | expectedRevision 加至少一个控制字段：deliveryEnabled、maxConcurrentDeliveries、maxConcurrentPerServer |
| GET /admin/config-status/{game}/{after} | 按区服游标查询已报告的应用版本和摘要 |
| POST /internal/config-applied/{game}/{server} | 游戏签名上报 version、contentHash；摘要不符及旧版本被拒绝 |

共享控制以版本比较更新并写审计，各实例每秒刷新；暂停会排空已领取的请求，不撤销在途经济操作。额度仍是各实例的并发额度，部署时须合并规划。Worker 忙时有界连续领取，空闲按 pollMs 退避；maxTasksPerTick 限制每轮工作量。

配置拉取附带 `canonicalContent`，接收端直接按其 UTF-8 字节核对 sha256，避免跨语言浮点序列化差异。游戏服必须原子应用并保存当前版本，再报告；任务 done 只代表通知确认。模拟接收端可用 `--hub-url` 演示启动补拉、乱序保护和应用回报。

## 密钥轮换与维护

未配置密钥标识时使用单密钥模式。启用轮换时，auth 配置 `jwtKeyId`、`jwtPreviousKeys` 和可选 `jwtLegacySecretEnv`；游戏出向对应 `outboundKeyId`、`outboundPreviousKeys`、`outboundLegacySecretEnv`；渠道对应 `keyId`、`previousKeys`、`legacySecretEnv`。验签窗口中的每项为 `{ "keyId": "previous", "secretEnv": "GAMEHUB_PREVIOUS_SECRET" }`，密钥只从环境读取。带标识的 HMAC 请求携带 `X-Key-Id` 并把标识纳入签名，JWT 使用标准 kid；轮换模式下，无标识请求只接受显式配置的 legacy 密钥。窗口结束后移除对应验签项，管理令牌通过 `admin_token.py --key-id` 签发。

`hub_admin.py archive` 可按保留天数、批次和总预算执行维护；[维护计划脚本](deploy/gamehub-maintain.ps1)与[Windows 服务脚本](deploy/gamehub-service.ps1)提供默认预览及显式执行入口。服务账号环境、目录权限和证书由部署系统配置。

`scripts/reconcile.py` 支持完整业务身份及 UTC 账期水位，`--case-ledger` 记录差异处置摘要，不自动补发。渠道下载和游戏权威账本导出须由私有接入实现。`tests/soak.py` 提供有界内存长稳验证，只使用自行新建的临时 MySQL；生产峰值、恢复时间、告警和证书轮换仍须在目标环境验收。

[变更记录](CHANGELOG.md) · [参与贡献](CONTRIBUTING.md) · [安全报告](SECURITY.md) · [第三方许可](THIRD_PARTY_NOTICES.md) · [MIT License](LICENSE)
