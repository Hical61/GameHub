/** @file DemoGameAdapter.h
 * @brief 演示渠道适配器，不对应真实支付渠道。
 */
#pragma once
#include "core/Config.h"
#include "core/GameAdapter.h"

namespace gamehub
{
	inline constexpr std::int64_t kMaxDemoAmountMinor = 1'000'000'000'000;
	/// @param body 演示订单对象。@return 检查并规范化后的订单；不接受额外字段。
	boost::json::object normalizeDemoOrder(const boost::json::object& body);

	class DemoGameAdapter final : public GameAdapter
	{
	public:
		/// @param config 游戏配置；适配器持有独立副本，调用后无需保留原配置。
		explicit DemoGameAdapter(const GameConfig& config) : config_(config)
		{
			if (config_.outboundSecret.size() < 32)
			{
				throw ConfigError("游戏出向 HMAC 密钥长度不够");
			}
		}

		/// @return 演示适配器绑定的游戏标识。
		std::string_view gameId() const override;
		/// @param request 签名需要的请求信息。@return HMAC-SHA256 签名。
		std::string signOutbound(const SignedRequest& request) const override;
		/// @return 当前出向签名的密钥标识；空值保持旧协议。
		std::string_view outboundKeyId() const override;
		/// @param request 完整请求；now 当前 UTC 秒。@return 内部请求的签名和时间戳是否有效。
		bool verifyInternalRequest(const SignedRequest& request, std::int64_t now) const override;

	private:
		GameConfig config_;
	};
} // namespace gamehub
