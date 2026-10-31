# 第三方依赖与许可

GameHub 自有源码使用 [MIT 许可](LICENSE)。下面列出配置来源和构建依赖，第三方组件按各自的许可使用。

## Hical 配置来源

`.clang-format` 来自 [Hical](https://github.com/Hical61/Hical)，`.editorconfig` 也参考了 Hical 的配置。构建使用的固定提交是 `5ba57c95575401bf36499094fcf42eb900707939`，相关 MIT 声明保留如下：

```text
MIT License

Copyright (c) 2026 hical

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 构建依赖

仓库内的 `cmake/patches/hical-response-body-limit.patch` 基于上述 Hical 提交，修改客户端读取上限、中间响应处理、连接池选项和错误类型。CMake 只把补丁应用到构建目录的副本，不写入调用者的 Hical 工作区；相关 Hical 版权和 MIT 声明继续保留。

| 组件 | 用途 | 上游许可入口 |
| --- | --- | --- |
| [Hical](https://github.com/Hical61/Hical) | HTTP 服务、客户端池、协程任务与中间件 | [MIT](https://github.com/Hical61/Hical/blob/5ba57c95575401bf36499094fcf42eb900707939/LICENSE) |
| [Boost](https://www.boost.org/) | JSON、Asio、MySQL、Charconv 等 | [Boost Software License 1.0](https://www.boost.org/LICENSE_1_0.txt) |
| [OpenSSL](https://www.openssl.org/) | HMAC、摘要及传输依赖 | [Apache License 2.0](https://github.com/openssl/openssl/blob/openssl-3.6.3/LICENSE.txt) |
| [zlib](https://zlib.net/) | Hical 压缩依赖 | [zlib 许可](https://zlib.net/zlib_license.html) |
| [picohttpparser](https://github.com/h2o/picohttpparser) | Hical 自带的 HTTP 解析器 | [上游文件许可声明](https://github.com/h2o/picohttpparser/blob/master/picohttpparser.c) |

仓库不提交依赖源码和二进制，`_deps/`、`.local/` 和构建目录都已加入忽略规则。CMake 安装时将 GameHub 的 MIT、这份说明，以及实际构建依赖的许可放入 `share/gamehub/licenses/<组件>/`，`manifest.tsv` 记录文件和 SHA-256。Hical 与内置 picohttpparser 的声明取自参与编译的源码副本；Boost、OpenSSL、zlib 优先取包管理器安装前缀中的 `share/<包>/copyright` 或 `share/licenses/<包>/`。内置 picohttpparser 的 `.c`、`.h` 首部声明分别完整保留。

自动查找支持 vcpkg 和 MSYS2/MinGW 的上述目录布局；安装好的 Hical 若未附 picohttpparser 的声明，或依赖使用其他目录布局，需要设置 `GAMEHUB_LICENSE_<组件大写>_FILES`，提供与本次依赖版本对应的原始许可文件，多个文件用分号分隔。例如 `GAMEHUB_LICENSE_PICOHTTPPARSER_FILES`；运行库组件中的连字符改为下划线，如 `GAMEHUB_LICENSE_GCC_RUNTIME_FILES`。缺少或空许可会导致配置失败，安装时还会核对许可摘要；`scripts/check-install.py` 同时验证各组件、许可正文和程序启动。配置过程不下载许可证或修改依赖目录。

## Windows 编译器运行库

MSVC 构建沿用 CMake `InstallRequiredSystemLibraries` 从 Visual Studio 可分发目录复制运行库。随包附带的 `cmake/licenses/msvc-runtime-2015-2022.rtf` 原样提取自 Visual Studio 2022 所附 `VC_redist.x64.exe` 14.40.33810.0 的 `license.rtf`，只在 v143 工具集下自动使用。原安装包 SHA-256 为 `3642e3f95d50cc193e4b5a0b0ffbf7fe2c08801517758b4c8aeb7105a091208a`，RTF SHA-256 为 `8099dc3cf9502c335da829e5c755948a12e3e6de490eb492a99deb673d883d8b`。其他工具集需通过 `GAMEHUB_LICENSE_MSVC_RUNTIME_FILES` 提供对应条款。

Microsoft 对这些文件的分发另有条款，见[运行库许可原文](https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/)和[官方分发说明](https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files)。附带条款不代替发布方核实自身的 Visual Studio 授权及适用分发条件。

MinGW 构建还收集 GCC 运行库的许可及运行库例外、MinGW CRT 和 winpthreads 的安装声明。这里只收集 GameHub 的编译和运行依赖，不复制数据库测试工具。

测试脚本从 [MySQL 官方归档](https://downloads.mysql.com/archives/community/) 获取 MySQL Community Server 8.0.16 Windows 工具，下载和解压内容只保存在 `.local/`。该归档附带的许可说明为 GPLv2 及其列出的额外许可，测试工具不随 GameHub 源码提交；如另行分发这些工具，应保留归档中的许可和第三方说明。GameHub 通过 Boost.MySQL 访问数据库，不链接归档中的 libmysqlclient。
