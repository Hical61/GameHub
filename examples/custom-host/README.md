# 独立宿主示例

这个可执行程序自行注册 `synthetic-game-v1` 和 `synthetic-channel-v1`，再把注册表交给公共 `Application`。公共默认工厂不需要知道使用方的项目，也不会扫描其他目录。

示例复用公开的演示 HMAC 协议，并增加一条完整的合成商品规则：只接受 `synthetic-coins`、`USD`、金额 `125`（最小货币单位）。适配器和渠道复制自己的配置，临时工厂目录销毁后注册表仍可使用。这是接入方式示例，真实渠道验签、业务规则和游戏权威事务由使用方在自己的宿主中实现。

## 构建与配置检查

先按[公共构建说明](../../README.md#构建)准备固定版本的 Hical、vcpkg 和编译器。在公共仓库根目录执行：

```powershell
$publicRoot = (Get-Location).Path
cmake -S examples/custom-host -B build-custom-host -G "Visual Studio 17 2022" -A x64 `
  "-DCMAKE_TOOLCHAIN_FILE=$publicRoot/_deps/vcpkg/scripts/buildsystems/vcpkg.cmake" `
  -DVCPKG_TARGET_TRIPLET=x64-windows "-DGAMEHUB_HICAL_SOURCE=$publicRoot/_deps/hical"
cmake --build build-custom-host --config RelWithDebInfo --target synthetic_gamehub_host --parallel 4

$env:GAMEHUB_HUBDB_PASSWORD = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_SYNTHETIC_ADMIN_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_SYNTHETIC_CALLBACK_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
$env:GAMEHUB_SYNTHETIC_OUTBOUND_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
./build-custom-host/RelWithDebInfo/synthetic_gamehub_host.exe `
  --config examples/custom-host/gamehub.example.json --check-config
```

`--check-config` 会实际解析配置并构造宿主注册表，成功返回 `0`，不连接数据库。这里生成的数据库密码只用于配置检查。正常启动前，先按[公共运行说明](../../README.md#运行)准备独立数据库、迁移和运行账号，并满足其中的触发器权限及 binlog 前提。

示例配置的库名是 `synthetic_hub`，运行账号是 `synthetic_runtime`；请按这两个值准备环境，或在自己的配置中改成已创建的 Hub 库与账号。该库只存 Hub 数据，不是游戏玩家库。

继续在刚才设置合成密钥的同一个 PowerShell 终端、公共仓库根目录执行。复制配置并填写实际数据库地址，用真正的运行账号密码替换配置检查用的随机密码。示例沿用公共管理令牌的 issuer/audience；令牌使用本宿主的密钥和游戏范围，直接保存到本地文件：

```powershell
New-Item -ItemType Directory -Path private/config -Force | Out-Null
Copy-Item examples/custom-host/gamehub.example.json private/config/synthetic-gamehub.json
$credential = Get-Credential -UserName synthetic_runtime -Message "输入合成宿主的 Hub 数据库账号密码"
$env:GAMEHUB_HUBDB_PASSWORD = $credential.GetNetworkCredential().Password
$syntheticAdminToken = python scripts/admin_token.py --subject synthetic-operator --role viewer --game synthetic-game `
    --secret-env GAMEHUB_SYNTHETIC_ADMIN_SECRET
if ($LASTEXITCODE -ne 0) { throw "合成宿主管理令牌签发失败。" }
Set-Content -LiteralPath private/config/synthetic-admin-token.txt -Value $syntheticAdminToken -Encoding utf8NoBOM -NoNewline -ErrorAction Stop
Remove-Variable syntheticAdminToken
$syntheticReceiverProcess = Start-Process -FilePath (Get-Command python -ErrorAction Stop).Source `
    -ArgumentList @('examples/demo_receiver.py', '--game-id', 'synthetic-game', '--server-id', '1', '--port', '9001',
        '--secret-env', 'GAMEHUB_SYNTHETIC_OUTBOUND_SECRET', '--database', '.local/synthetic-host-receiver.db') `
    -WorkingDirectory $PWD.Path -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $PWD.Path 'private/config/synthetic-receiver.stdout.log') `
    -RedirectStandardError (Join-Path $PWD.Path 'private/config/synthetic-receiver.stderr.log') -PassThru
try {
    ./build-custom-host/RelWithDebInfo/synthetic_gamehub_host.exe --config private/config/synthetic-gamehub.json
    if ($LASTEXITCODE -ne 0) { throw "合成宿主运行失败，请检查本地日志。" }
} finally {
    if (-not $syntheticReceiverProcess.HasExited) { Stop-Process -InputObject $syntheticReceiverProcess }
}
```

接收端继承了当前终端的出向密钥，退出宿主后只清理保存的这个进程。新开的并列终端不会继承密钥；在新终端启动或重启时，必须从自己的安全配置恢复相同值，不要重新生成。不要输出或提交令牌文件。在另一个终端切换到公共仓库根目录后，可直接读取本宿主的控制状态：

```powershell
python scripts/hub_admin.py --url http://127.0.0.1:8443 --token-file private/config/synthetic-admin-token.txt `
    control-get --game synthetic-game
```

此宿主的回调入口是 `/api/callback/synthetic-game/synthetic-channel`。演示订单除公共协议的七个字段外不增加字段，其中 `productId="synthetic-coins"`、`currency="USD"`、`amountMinor=125`；验签、确认和重试规则见 [API 文档](../../docs/api.md)。示例没有真实发奖。

## 放到自己的项目

把本目录复制到独立宿主工程，配置时显式设置 `-DGAMEHUB_SOURCE_DIR=<公共源码绝对路径>`。Hical 和 vcpkg 路径仍通过普通 CMake 参数提供；示例默认使用该公共源码下的依赖清单，也允许指定自己的 `VCPKG_MANIFEST_DIR`。

宿主只需要维护三部分：自己的入口及工厂注册、自己的配置、游戏与渠道实现。通过 `add_subdirectory` 引入公共源码并链接 `GameHub::service`、`GameHub::bootstrap`。示例关闭公共可执行程序、测试和安装规则，构建只生成宿主所需目标；发布内容由宿主决定。

这里没有动态插件发现或私有源码自动加载。将新的适配器明确加入宿主目标，并在自己的 `AdapterCatalog` 中登记即可。公共 `GameAdapter` 当前提供签名与内部验签接口，出向 JSON POST 和业务 ACK 契约由公共 Worker 实现；需要改变通信契约时应同步扩展接口与验证，不能只改注册名称。
