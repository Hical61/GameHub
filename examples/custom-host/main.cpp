/** @file main.cpp
 * @brief 独立宿主自行登记游戏和渠道，公共默认目录保持独立。
 */
#include "bootstrap/AdapterCatalog.h"
#include "core/Config.h"
#include "games/DemoGameAdapter.h"
#include "games/HmacJsonChannel.h"
#include "service/Application.h"
#include <core/Log.h>
#include <memory>
#include <string_view>

namespace
{
	constexpr std::string_view kSyntheticGameType = "synthetic-game-v1";
	constexpr std::string_view kSyntheticChannelType = "synthetic-channel-v1";
	constexpr char kSyntheticProductId[] = "synthetic-coins";
	constexpr char kSyntheticCurrency[] = "USD";
	constexpr std::int64_t kSyntheticAmountMinor = 125;

	/// @param body 演示 HMAC 渠道解析的订单对象。@return 同时满足演示字段和合成商品规则的规范化订单。
	boost::json::object normalizeSyntheticOrder(const boost::json::object& body)
	{
		auto order = gamehub::normalizeDemoOrder(body);
		if (order.at("productId").as_string() != kSyntheticProductId
			|| order.at("currency").as_string() != kSyntheticCurrency
			|| order.at("amountMinor").as_int64() != kSyntheticAmountMinor)
		{
			throw gamehub::ApiError(400, "合成宿主只接受约定的商品、币种和金额");
		}
		return order;
	}

	/// @param config 已通过通用解析的宿主配置。@return 自持有游戏/渠道配置及规范化函数的注册表。
	gamehub::GameRegistry createHostRegistry(const gamehub::Config& config)
	{
		gamehub::AdapterCatalog catalog;
		catalog.addGame(std::string(kSyntheticGameType),
						[](const gamehub::GameConfig& game)
						{
							// 演示实现复制配置；注册表不借用本函数内的目录或调用方配置。
							return std::make_unique<gamehub::DemoGameAdapter>(game);
						},
						normalizeSyntheticOrder,
						{});
		catalog.addChannel(std::string(kSyntheticChannelType),
						   [](const gamehub::GameConfig& game,
							  const gamehub::ChannelConfig& channel,
							  const gamehub::AdapterCatalog::PaymentNormalizer& normalize)
						   {
							   return std::make_unique<gamehub::HmacJsonChannel>(game, channel, normalize);
						   });
		return catalog.buildRegistry(config);
	}
} // namespace

/// @param argc/argv 配置路径及可选 --check-config。@return 成功为 0，用法错误为 2，启动或配置错误为 1。
int main(int argc, char** argv)
{
	try
	{
		if (argc < 3 || argc > 4 || std::string_view(argv[1]) != "--config"
			|| (argc == 4 && std::string_view(argv[3]) != "--check-config"))
		{
			HICAL_LOG_INFO("用法：synthetic_gamehub_host --config <配置文件> [--check-config]");
			return argc == 2 && std::string_view(argv[1]) == "--help" ? 0 : 2;
		}
		auto config = gamehub::loadConfig(argv[2]);
		auto registry = createHostRegistry(config);
		if (argc == 4)
		{
			HICAL_LOG_INFO("合成宿主配置和注册目录检查通过");
			return 0;
		}
		std::make_shared<gamehub::Application>(std::move(config), std::move(registry))->run();
		return 0;
	}
	catch (const gamehub::ConfigError& error)
	{
		HICAL_LOG_ERROR("宿主配置检查失败：{}", error.what());
	}
	catch (const gamehub::ApiError& error)
	{
		HICAL_LOG_ERROR("宿主配置字段错误：{}", error.what());
	}
	catch (const std::exception&)
	{
		// 解析异常可能带配置原文，固定提示避免输出密钥或本地配置内容。
		HICAL_LOG_ERROR("合成宿主启动失败，请检查配置、环境变量和依赖");
	}
	return 1;
}
