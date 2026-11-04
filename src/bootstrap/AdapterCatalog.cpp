/** @file AdapterCatalog.cpp
 * @brief 组装具体游戏与渠道；工厂负责协议专用配置检查。
 */
#include "AdapterCatalog.h"
#include "games/DemoGameAdapter.h"
#include "games/HmacJsonChannel.h"

namespace gamehub
{
	void AdapterCatalog::addGame(std::string type,
								 GameFactory factory,
								 PaymentNormalizer normalize,
								 std::string legacyChannel)
	{
		if (!validKey(type, 32) || !factory || !normalize || (!legacyChannel.empty() && !validKey(legacyChannel, 32))
			|| !games_
					.emplace(std::move(type),
							 GameDefinition {std::move(factory), std::move(normalize), std::move(legacyChannel)})
					.second)
		{
			throw std::invalid_argument("游戏适配器工厂无效或重复");
		}
	}

	void AdapterCatalog::addChannel(std::string type, ChannelFactory factory)
	{
		if (!validKey(type, 32) || !factory || !channels_.emplace(std::move(type), std::move(factory)).second)
		{
			throw std::invalid_argument("渠道适配器工厂无效或重复");
		}
	}

	void AdapterCatalog::validate(const Config& config) const
	{
		buildRegistry(config);
	}

	GameRegistry AdapterCatalog::buildRegistry(const Config& config) const
	{
		GameRegistry registry;
		for (const auto& game : config.games)
		{
			const auto definition = games_.find(game.adapter);
			if (definition == games_.end())
			{
				throw ConfigError("配置了未注册的游戏适配器");
			}
			registry.add(definition->second.factory(game));
			auto channels = game.channels;
			if (channels.empty())
			{
				const auto& channel = definition->second.legacyChannel;
				if (channel.empty())
				{
					throw ConfigError("该游戏适配器需要显式配置 channels");
				}
				channels.push_back({channel, game.adapter, channel, game.callbackSecret, {}});
			}
			for (const auto& channel : channels)
			{
				const auto factory = channels_.find(channel.adapter);
				if (factory == channels_.end())
				{
					throw ConfigError("配置了未注册的渠道适配器");
				}
				if (!validKey(channel.nameSpace, 32))
				{
					throw ConfigError("渠道业务命名空间无效");
				}
				registry.addChannel(game.id, channel.id, factory->second(game, channel, definition->second.normalize));
			}
		}
		return registry;
	}

	AdapterCatalog createBuiltinCatalog()
	{
		AdapterCatalog catalog;
		catalog.addGame(
			"demo-hmac-v1",
			[](const GameConfig& game)
			{
				return std::make_unique<DemoGameAdapter>(game);
			},
			normalizeDemoOrder,
			"demo");
		catalog.addChannel(
			"demo-hmac-v1",
			[](const GameConfig& game, const ChannelConfig& channel, const AdapterCatalog::PaymentNormalizer& normalize)
			{
				return std::make_unique<HmacJsonChannel>(game, channel, normalize);
			});
		return catalog;
	}
} // namespace gamehub
