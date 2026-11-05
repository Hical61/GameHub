/** @file HmacJsonChannel.h
 * @brief 内置 JSON/HMAC 渠道协议，游戏字段规则通过规范化函数注入。
 */
#pragma once
#include "core/Config.h"
#include "core/GameAdapter.h"

namespace gamehub
{
	class HmacJsonChannel final : public ChannelAdapter
	{
	public:
		using Normalizer = std::function<boost::json::object(const boost::json::object&)>;

		/// @param game 游戏路由；channel 渠道实例及凭据；normalize 游戏字段规范化函数。@return 构造后自持有配置数据。
		HmacJsonChannel(const GameConfig& game, ChannelConfig channel, Normalizer normalize);
		/// @param request 原始请求，只在调用期间使用；now 当前 UTC 秒。@return 检查通过、自持有的到账通知。
		Submission parseCallback(const CallbackRequest& request, std::int64_t now) const override;
		/// @param result 已受理、重复或固定错误。@return 演示协议的 JSON 应答。
		CallbackReply reply(const CallbackResult& result) const override;

	private:
		GameConfig game_;
		ChannelConfig channel_;
		Normalizer normalize_;
	};
} // namespace gamehub
