/** @file AdapterCatalog.h
 * @brief 启动时统一注册适配器工厂，核心配置不维护具体游戏白名单。
 */
#pragma once
#include "core/Config.h"
#include "core/GameAdapter.h"

namespace gamehub
{
	class AdapterCatalog
	{
	public:
		using GameFactory = std::function<std::unique_ptr<GameAdapter>(const GameConfig&)>;
		using PaymentNormalizer = std::function<boost::json::object(const boost::json::object&)>;
		using ChannelFactory = std::function<
			std::unique_ptr<ChannelAdapter>(const GameConfig&, const ChannelConfig&, const PaymentNormalizer&)>;

		/// @param type 类型；factory 游戏通信构造；normalize 该游戏的订单约束；legacyChannel 旧格式默认渠道。@return 无，重复注册时抛异常。
		void addGame(std::string type, GameFactory factory, PaymentNormalizer normalize, std::string legacyChannel);
		/// @param type 渠道类型；factory 构造及专用配置校验，使用传入的游戏规则校验规范化订单。@return 无，重复注册时抛异常。
		void addChannel(std::string type, ChannelFactory factory);
		/// @param config 完整配置。@return 无；未知类型或不合法绑定在连接数据库之前抛 ConfigError。
		void validate(const Config& config) const;
		/// @param config 完整配置。@return 各适配器自持有配置、运行期只读的注册表；同时执行全部构造校验。
		GameRegistry buildRegistry(const Config& config) const;

	private:
		struct GameDefinition
		{
			GameFactory factory;
			PaymentNormalizer normalize;
			std::string legacyChannel;
		};

		std::unordered_map<std::string, GameDefinition> games_;
		std::unordered_map<std::string, ChannelFactory> channels_;
	};

	/// @return 内置游戏与渠道协议的工厂目录；新增实现只需在应用组装模块登记。
	AdapterCatalog createBuiltinCatalog();
} // namespace gamehub
