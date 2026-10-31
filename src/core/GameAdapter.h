/** @file GameAdapter.h
 * @brief 各游戏实现自己的渠道验签、回调解析和出向签名。
 */
#pragma once
#include "Domain.h"
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
	};

	class GameAdapter
	{
	public:
		virtual ~GameAdapter() = default;
		/// @return 适配器所属的游戏标识；返回的视图与适配器同寿命。
		virtual std::string_view gameId() const = 0;
		/// @param channel 渠道；request 请求视图；now 当前 UTC 秒。@return 验签并检查字段后的业务事件，事件持有自己的数据。
		virtual Submission parseCallback(std::string_view channel,
										 const SignedRequest& request,
										 std::int64_t now) const = 0;
		/// @param request 请求视图，只在调用期间使用。@return 出向签名字符串。
		virtual std::string signOutbound(const SignedRequest& request) const = 0;
		/// @param request 请求视图；now 当前 UTC 秒。@return 游戏服拉取请求是否可信。
		virtual bool verifyInternalRequest(const SignedRequest& request, std::int64_t now) const = 0;
	};

	class GameRegistry
	{
	public:
		/// @param adapter 转交给注册表持有的适配器；游戏重复时抛异常。无返回值，只在启动时调用。
		void add(std::unique_ptr<GameAdapter> adapter);
		/// @param id 游戏标识。@return 已注册的适配器；未注册时抛 404，运行期间只查询、不修改注册表。
		const GameAdapter& find(const std::string& id) const;

	private:
		std::unordered_map<std::string, std::unique_ptr<GameAdapter>> adapters_;
	};
} // namespace gamehub
