# GameHub

GameHub 是基于 [Hical](https://github.com/Hical61/Hical)、C++20 协程和 MySQL 的多游戏服务端中台，负责渠道回调、到账通知、公告和配置分发。商品、活动和发奖逻辑由游戏服处理。

当前版本 **0.1.0**，内置 `demo-hmac-v1` 演示协议，尚未接入真实支付渠道和游戏发奖。

## 功能

- 多游戏、多渠道接入，验签后将事件、逐服任务和审计记录一起提交，按业务身份持久化去重。
- 异步投递、退避重试、租约回收和人工重排，支持按游戏及区服分配并发额度。
- 配置版本分发、JWT 管理权限、游戏范围隔离和独立回调限流。
- 任务归档、全局及游戏级统计、健康探针和 Prometheus 指标。

通知可能重复投递，游戏服须在业务事务中持久化去重，提交后再确认。

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

使用独立的 Hub 数据库，最低 MySQL 8.0.16。迁移账号需要 DDL、TRIGGER 权限，保留触发器的 DEFINER 账号；运行账号只授予本库的 SELECT、INSERT、UPDATE、DELETE。

创建空库 `gamehub` 后依次执行 [001](migrations/001_initial.sql)、[002](migrations/002_operations.sql)。已有结构版本 1 或旧版本 2 的库只执行 002，版本 3 不重跑。升级前停止全部写入并备份。

在仓库根目录执行，按实际环境修改账号和地址；存量升级时将数组改为仅含 002：

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

版本结果应为 `3`。迁移必须使用首错停止的非交互客户端，不加 `--force`，不用交互式 `SOURCE`；DDL 失败不能整体回滚，须检查已执行的步骤。

将 [配置示例](config/gamehub.example.json) 复制为 `config/gamehub.json`，填写数据库和游戏服内网地址，再设置数据库密码及三个独立随机密钥：

```powershell
$credential = Get-Credential -UserName gamehub -Message "输入 Hub 数据库账号密码"
$env:GAMEHUB_HUBDB_PASSWORD = $credential.GetNetworkCredential().Password
$env:GAMEHUB_ADMIN_JWT_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_DEMO_CALLBACK_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_DEMO_OUTBOUND_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
./build-msvc/RelWithDebInfo/gamehub.exe --config config/gamehub.json --check-config
./build-msvc/RelWithDebInfo/gamehub.exe --config config/gamehub.json
```

`--check-config` 只检查配置和环境变量，不连接数据库。启动后可通过 `/admin/ready` 检查数据库是否就绪。

联调时，用 `python examples/demo_receiver.py --port 9001` 启动模拟游戏服，与 Hub 共用同一 `GAMEHUB_DEMO_OUTBOUND_SECRET`。管理令牌通过 `python scripts/admin_token.py --subject local-operator --role operator --game demo` 生成，请求时放入 `Authorization: Bearer ...`。

## 接入与部署

- 扩展 `games`、`channels`、`servers` 数组接入多游戏、多商户和多区服。`worker.games` 指定本进程负责的游戏，配额只在当前进程生效。
- `ChannelAdapter` 处理渠道协议，`GameAdapter` 处理游戏通信。实际项目适配和游戏数据库访问在私有集成中维护，通过公共接口登记。
- 公网代理只开放回调，管理和配置接口放内网。出向 HTTP 仅用于可信内网，跨不可信网络需加 TLS / mTLS。
- 密钥、令牌、本地配置和玩家数据不要提交到仓库。

[变更记录](CHANGELOG.md) · [参与贡献](CONTRIBUTING.md) · [安全报告](SECURITY.md) · [第三方许可](THIRD_PARTY_NOTICES.md) · [MIT License](LICENSE)
