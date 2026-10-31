/** @file main.cpp
 * @brief GameHub 命令行入口。
 */
#include "core/Config.h"
#include "core/Domain.h"
#include "service/Application.h"
#include <core/Log.h>
#include <memory>
#include <string_view>

int main(int argc, char** argv)
{
	try
	{
		if (argc == 2 && std::string_view(argv[1]) == "--version")
		{
			HICAL_LOG_INFO("GameHub 0.1.0");
			return 0;
		}
		if (argc < 3 || std::string_view(argv[1]) != "--config" || argc > 4
			|| (argc == 4 && std::string_view(argv[3]) != "--check-config"))
		{
			HICAL_LOG_INFO("用法：gamehub --config config/gamehub.json [--check-config]，或 gamehub --version");
			return argc == 2 && std::string_view(argv[1]) == "--help" ? 0 : 2;
		}
		auto config = gamehub::loadConfig(argv[2]);
		if (argc == 4)
		{
			HICAL_LOG_INFO("配置和环境变量检查通过");
			return 0;
		}
		std::make_shared<gamehub::Application>(std::move(config))->run();
		return 0;
	}
	catch (const gamehub::ConfigError& error)
	{
		HICAL_LOG_ERROR("配置检查失败：{}", error.what());
	}
	catch (const gamehub::ApiError& error)
	{
		HICAL_LOG_ERROR("配置字段错误：{}", error.what());
	}
	catch (const std::exception&)
	{
		// JSON 异常里可能带着配置原文，这里只输出固定提示。
		HICAL_LOG_ERROR("启动失败，请检查配置格式、环境变量、监听地址和依赖");
	}
	return 1;
}
