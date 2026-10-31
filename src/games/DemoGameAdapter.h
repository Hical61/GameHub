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
		/// @param config 只读游戏配置，必须比适配器活得更久。
		explicit DemoGameAdapter(const GameConfig& config) : config_(config)
		{
		}

		/// @return 演示适配器绑定的游戏标识。
		std::string_view gameId() const override;
		/// @param channel 只支持 demo；request 请求内容；now 当前 UTC 秒。@return 检查通过的演示订单事件。
		Submission parseCallback(std::string_view channel,
								 const SignedRequest& request,
								 std::int64_t now) const override;
		/// @param request 签名需要的请求信息。@return HMAC-SHA256 签名。
		std::string signOutbound(const SignedRequest& request) const override;
		/// @param request 完整请求；now 当前 UTC 秒。@return 内部请求的签名和时间戳是否有效。
		bool verifyInternalRequest(const SignedRequest& request, std::int64_t now) const override;

	private:
		const GameConfig& config_;
	};
} // namespace gamehub
