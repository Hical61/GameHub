# GameHub

GameHub 是一个多游戏服务端中台，使用 [Hical](https://github.com/Hical61/Hical)、C++20 协程和 MySQL。它接收渠道回调，把到账通知、公告和配置通知发给对应的游戏服；商品、活动和发奖逻辑由游戏服处理。

**当前版本为 0.1.0，可以跑通演示流程，还没有接入真实支付渠道。** 目前只带 `demo-hmac-v1` 演示适配器。正式接入前还要完成渠道联调、游戏服发奖事务和部署测试。在线人数和排行榜等功能，要等首款游戏的数据结构确定后再做。

```text
渠道 → 验签与校验 → [事务：业务事件 + 逐服任务 + 审计] → 返回已受理
                                   ↓
                   Worker 短事务领取 → HTTP 投递 → 检查业务确认
                                   ↓ 失败或中断
                         退避重试 / 租约回收 / 人工重排
```

## 已实现

- 从 JSON 读取配置，从环境变量读取密钥；启动时检查游戏和区服是否重复，并提供存活、就绪探针。
- 用 `GameAdapter` / `GameRegistry` 接入不同游戏。演示签名会检查时间、请求方法、路径和请求体摘要。
- 相同业务键和内容返回原事件，内容不同返回 409；幂等记录保存在数据库中。
- 事件、各区服的投递任务和审计记录在同一事务提交。广播以首次受理时的区服列表为准。
- Worker 用 `SKIP LOCKED` 领取任务，发 HTTP 时不占用数据库连接和行锁。任务失败后退避重试，租约过期后自动回收。
- 游戏服必须返回匹配的 `taskId` 和 `committed=true`，Hub 才会把任务标为完成。
- 管理接口使用带有效期的 JWT，区分 viewer/operator 和游戏范围；失败任务可以人工重排，并留下审计记录。
- 配置按版本发布，已发布的内容不能修改。游戏服通过签名请求拉取配置。
- 出向响应在读取时限制字节数，超限关闭连接并重试，不接受部分响应作为确认。
- 出向请求会继续读取 `100/103` 等中间响应后的最终确认，复用连接时保持请求与响应对应。
- 提供分业务限流、事务计数、完成任务归档、载荷清理和 Prometheus 指标。
- 带有真实 MySQL 故障测试、容量与恢复测试、备份恢复演练和合成流水对账工具。

## 构建

依赖：CMake 3.28+、C++20 编译器、Boost 1.90+（JSON、Asio、MySQL、Charconv）、OpenSSL、zlib 和 Git。开启测试时还需要 Python 3.10+。数据库最低要求为 MySQL 8.0.16。

Windows 下已用 GCC 15.2 / Boost 1.90 和 MSVC 2022 / Boost 1.91 构建，并跑过核心测试和完整 MySQL 集成测试。

以下命令使用 PowerShell：

```powershell
git clone https://github.com/Hical61/GameHub.git
Set-Location GameHub
git clone --filter=blob:none --no-checkout https://github.com/Hical61/Hical.git _deps/hical
git -C _deps/hical checkout --detach 5ba57c95575401bf36499094fcf42eb900707939
git clone --filter=blob:none --no-checkout https://github.com/microsoft/vcpkg.git _deps/vcpkg
git -C _deps/vcpkg checkout --detach 301856f5f2824f788a3ffa6332293861cccd23b4
& ./_deps/vcpkg/bootstrap-vcpkg.bat -disableMetrics
```

根目录 `vcpkg.json` 固定依赖基线，CMake 配置时会安装所需库。首次构建需要联网，耗时取决于依赖缓存；`_deps/` 和构建产物不会纳入源码提交。MSVC 构建：

```powershell
cmake -S . -B build-msvc -G "Visual Studio 17 2022" -A x64 `
  "-DCMAKE_TOOLCHAIN_FILE=$PWD/_deps/vcpkg/scripts/buildsystems/vcpkg.cmake" `
  -DVCPKG_TARGET_TRIPLET=x64-windows `
  "-DGAMEHUB_HICAL_SOURCE=$PWD/_deps/hical"
cmake --build build-msvc --config RelWithDebInfo --parallel 4
ctest --test-dir build-msvc -C RelWithDebInfo --output-on-failure
```

```powershell
# MSYS2 MinGW：编译和运行使用同一工具链的 DLL
$env:PATH = "C:\msys64\mingw64\bin;" + $env:PATH
cmake -S . -B build -G Ninja `
  -DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/gcc.exe `
  -DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/g++.exe `
  -DCMAKE_PREFIX_PATH=C:/msys64/mingw64 `
  "-DGAMEHUB_HICAL_SOURCE=$PWD/_deps/hical"
cmake --build build --parallel 4
ctest --test-dir build --output-on-failure
```

如果本机缺少 MSVC 依赖，可以把它们装到项目目录，保留全局 vcpkg 的现有版本：

```powershell
./scripts/setup-native.ps1 -VcpkgRoot C:/vcpkg
cmake -S . -B build-msvc-native -G "Visual Studio 17 2022" -A x64 `
  -DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake `
  "-DVCPKG_INSTALLED_DIR=$PWD/.local/native-installed" `
  -DVCPKG_MANIFEST_MODE=OFF "-DGAMEHUB_HICAL_SOURCE=$PWD/_deps/hical"
cmake --build build-msvc-native --config RelWithDebInfo --parallel 4
```

源码构建固定使用上述 Hical 提交。CMake 会检查相关文件，把依赖复制到构建目录的 `_deps/hical-response-limit/`，再应用仓库内的 [HTTP 客户端补丁](cmake/patches/hical-response-body-limit.patch)，限制响应大小并修正中间响应处理。补丁只应用于构建副本，提供的 Hical 工作区不会被修改；不要直接换成另一个有未提交改动的 Hical 开发目录。

也可使用已安装、带响应上限接口的 Hical：不指定 `GAMEHUB_HICAL_SOURCE`，通过 `CMAKE_PREFIX_PATH` 定位 `hicalConfig.cmake`。缺少所需接口时，配置阶段会报错。

## 安装

构建完成后，可以把程序安装到单独的目录：

```powershell
cmake --install build-msvc --config RelWithDebInfo --prefix stage
python scripts/check-install.py --build-dir build-msvc --config RelWithDebInfo
```

Windows 安装会把依赖 DLL 和所需的 MSVC 运行库一起放入 `bin/`。配置示例位于 `config/`，迁移脚本位于 `share/gamehub/migrations/`。第二条命令会另建临时安装目录，在干净 PATH 下运行 `--version`，成功后清理，失败时保留安装目录。

## 先跑完整演示验证

集成测试需要 Python 3.10+ 和 MySQL 8.0.16+ 的 `mysqld`、`mysql`，不需要安装 Python 第三方包。脚本按编号执行全部迁移，创建临时数据库，使用随机端口和测试密钥，再启动模拟游戏服。**它不连接已有 MySQL，也不需要你提供数据库密码。** 成功后清理临时目录；失败时停止子进程，并留下脱敏日志和诊断信息，方便排查。

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -u tests/integration.py --binary build/gamehub.exe `
  --mysql-bin "C:/Program Files/MySQL/MySQL Server 9.0/bin"
```

使用 MSVC 构建时，把程序路径改为 `build-msvc/RelWithDebInfo/gamehub.exe`。存储测试程序必须位于服务程序同目录。

## 手工运行

1. 为 Hub 准备独立的 MySQL 8.0.16+ 数据库，空库依次执行 [001](migrations/001_initial.sql) 和 [002](migrations/002_operations.sql)。当前结构版本为 2；已有版本 1 的库，先停止所有 Hub 写入，再备份并执行 002。迁移和运行使用不同账号，运行账号仍只授予该库的 SELECT / INSERT / UPDATE / DELETE。迁移账号也是触发器的 DEFINER，需要保留。
2. 复制 `config/gamehub.example.json` 为 `config/gamehub.json`，填写数据库与游戏服内网端点。文件必须是标准 JSON。
3. 设置环境变量。数据库密码必须与账号一致，其余密钥分别生成至少 32 字节的独立随机值。

```powershell
$credential = Get-Credential -UserName gamehub -Message "输入 Hub 数据库账号密码"
$env:GAMEHUB_HUBDB_PASSWORD = $credential.GetNetworkCredential().Password
$env:GAMEHUB_ADMIN_JWT_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_DEMO_CALLBACK_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_DEMO_OUTBOUND_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
./build-msvc/RelWithDebInfo/gamehub.exe --config config/gamehub.json --check-config
./build-msvc/RelWithDebInfo/gamehub.exe --config config/gamehub.json
```

`--check-config` 只检查配置，不连接数据库。启动后，`/admin/ready` 会在迁移检查通过时返回 200，失败时返回 503。数据库恢复后会自动重试检查。

`worker.maxResponseBodyBytes` 默认 16384 字节，可设置为 64 到 1048576 字节。上限对 Content-Length、chunked 解码结果和 EOF 响应都有效，超限记为 `response_too_large`。配置启动后固定。

运行 `python examples/demo_receiver.py --port 9001` 可以启动模拟游戏服。它要使用与 Hub 相同的 `GAMEHUB_DEMO_OUTBOUND_SECRET`，用 SQLite 演示持久化去重和记账，不处理真实游戏业务。

运行 `python scripts/admin_token.py --subject local-operator --role operator --game demo` 生成管理令牌，默认有效 15 分钟。请求时放到 `Authorization: Bearer ...` 中。密钥、令牌和本地配置不要提交到仓库。

## 结构

```text
src/core/       配置、输入边界、摘要、适配抽象与注册表
src/games/      演示适配器
src/storage/    连接租约、事件、投递、配置与审计事务
src/service/    HTTP、认证、周期任务与 Worker
migrations/     显式数据库迁移
tests/          核心、存储、端到端故障验证
examples/       持久化幂等模拟接收端
scripts/        管理令牌、测试依赖、备份演练与合成对账工具
deploy/         代理、本地数据库和 Prometheus 示例
```

## 文档

- [变更记录](CHANGELOG.md)
- [参与贡献](CONTRIBUTING.md)
- [安全问题报告](SECURITY.md)
- [第三方依赖与许可](THIRD_PARTY_NOTICES.md)

Hub 使用一个事件循环，多个投递槽位通过协程并发工作；也可以启动多个相同配置的进程，通过数据库租约领取任务。部署时，公网代理只开放回调，管理和配置拉取接口放在内网。出向 HTTP 目前只适合可信内网，跨不可信网络前需要加 TLS / mTLS。

MIT 许可，见 [LICENSE](LICENSE)。
