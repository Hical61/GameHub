/** @file GameAdapter.h
 * @brief 渠道协议与游戏通信分别注册，核心不依赖具体实现。
 */
#pragma once
#include "Domain.h"
#include <functional>
#include <memory>
#include <unordered_map>

namespace gamehub
{
	struct SignedRequest
	{
		std::string_view timestamp;
		std::string_view signature;
		std::string_view method;
		std::string_view path;
		std::string_view body;
		std::string_view keyId {};
	};

	struct CallbackRequest
	{
		std::string_view method;
		std::string_view path;
		std::string_view target;
		std::string_view body;
		// 原始视图及请求头访问器仅在 parseCallback 调用期间有效，不得跨请求保存。
		std::function<std::string_view(std::string_view)> header;
	};

	struct CallbackResult
	{
		int status = 200;
		std::uint64_t eventId = 0;
		bool duplicate = false;
		std::string message;
	};

	struct CallbackReply
	{
		int status = 200;
		std::string contentType;
		std::string body;
	};

	class ChannelAdapter
	{
	public:
		virtual ~ChannelAdapter() = default;
		/// @param request 原始请求视图，仅调用期间有效；now 当前 UTC 秒。@return 验签并规范化后自持有数据的事件。
		virtual Submission parseCallback(const CallbackRequest& request, std::int64_t now) const = 0;
		/// @param result 持久受理结果或固定错误类别。@return 本渠道协议的应答，不暴露依赖异常细节。
		virtual CallbackReply reply(const CallbackResult& result) const = 0;
	};

	class GameAdapter
	{
	public:
		virtual ~GameAdapter() = default;
		/// @return 适配器所属的游戏标识；返回的视图与适配器同寿命。
		virtual std::string_view gameId() const = 0;
		/// @param request 请求视图，只在调用期间使用。@return 出向签名字符串。
		virtual std::string signOutbound(const SignedRequest& request) const = 0;

		/// @return 当前出向密钥标识；空值表示保持旧协议，不发送 X-Key-Id。
		virtual std::string_view outboundKeyId() const
		{
			return {};
		}

		/// @param request 请求视图；now 当前 UTC 秒。@return 游戏服拉取请求是否可信。
		virtual bool verifyInternalRequest(const SignedRequest& request, std::int64_t now) const = 0;
	};

	class GameRegistry
	{
	public:
		/// @param adapter 转交给注册表持有的适配器；游戏重复时抛异常。无返回值，只在启动时调用。
		void add(std::unique_ptr<GameAdapter> adapter);
		/// @param gameId 已注册游戏；channelId 渠道实例；adapter 渠道实现。@return 无；重复或非法标识时抛异常，只在启动时调用。
		void addChannel(std::string gameId, std::string channelId, std::unique_ptr<ChannelAdapter> adapter);
		/// @param id 游戏标识。@return 已注册的适配器；未注册时抛 404，运行期间只查询、不修改注册表。
		const GameAdapter& find(const std::string& id) const;
		/// @param gameId 游戏；channelId 渠道实例标识。@return 渠道实现；不存在时抛 404，运行期间只读。
		const ChannelAdapter& channel(const std::string& gameId, const std::string& channelId) const;

	private:
		std::unordered_map<std::string, std::unique_ptr<GameAdapter>> adapters_;
		std::unordered_map<std::string, std::unordered_map<std::string, std::unique_ptr<ChannelAdapter>>> channels_;
	};
} // namespace gamehub
