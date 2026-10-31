# 把 MSVC 依赖装到项目目录，保留 vcpkg 全局 installed 目录不动。
param([string]$VcpkgRoot = 'C:\vcpkg')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$localRoot = Join-Path $projectRoot '.local'
$env:VCPKG_DISABLE_METRICS = '1'
& (Join-Path $VcpkgRoot 'vcpkg.exe') install --classic boost-asio boost-json boost-mysql boost-charconv openssl zlib `
    --triplet x64-windows --host-triplet x64-windows `
    "--x-install-root=$localRoot/native-installed" `
    "--x-buildtrees-root=$localRoot/native-buildtrees" `
    "--x-packages-root=$localRoot/native-packages"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Write-Host '依赖装好了，配置 CMake 时加上：'
Write-Host "-DVCPKG_INSTALLED_DIR=$localRoot/native-installed -DVCPKG_MANIFEST_MODE=OFF"
