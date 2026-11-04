/** @file IngressLimits.cpp
 * @brief 已知游戏使用固定的独立限流器，未知路径共用兜底桶，避免按任意输入创建桶。
 */
#include "IngressLimits.h"
#include <core/HttpRequest.h>
#include <core/HttpResponse.h>
#include <core/RateLimiter.h>
#include <unordered_map>

namespace gamehub
{
	namespace
	{
		constexpr double kRatePerSecond = 100;
		constexpr double kRateBurst = 200;
		constexpr std::string_view kCallbackPrefix = "/api/callback/";

		bool invalidPath(std::string_view path)
		{
			return path.empty() || path.front() != '/' || path.find_first_of("%+\\") != std::string_view::npos
				   || path.find("//") != std::string_view::npos || path.find("/./") != std::string_view::npos
				   || path.find("/../") != std::string_view::npos || path.ends_with("/.") || path.ends_with("/..");
		}

		std::string callbackGame(std::string_view path)
		{
			if (!path.starts_with(kCallbackPrefix))
			{
				return {};
			}
			auto rest = path.substr(kCallbackPrefix.size());
			return std::string(rest.substr(0, rest.find('/')));
		}
	} // namespace

	hical::SyncBeforeHandler makeIngressLimits(const Config& config)
	{
		std::unordered_map<std::string, hical::SyncBeforeHandler> callbacks;
		for (const auto& game : config.games)
		{
			callbacks.emplace(
				game.id,
				hical::makeRateLimiterMiddleware({.config = {static_cast<double>(game.callbackRatePerSecond),
															 static_cast<double>(game.callbackBurst)},
												  .keyExtractor = [](const hical::HttpRequest&)
												  {
													  return std::string("game");
												  }}));
		}
		auto fallback = hical::makeRateLimiterMiddleware(
			{.config = {kRatePerSecond, kRateBurst},
			 .keyExtractor = [](const hical::HttpRequest& request)
			 {
				 return request.path().starts_with(kCallbackPrefix)
							? std::string("unknown-callback")
							: (request.path().starts_with("/admin/") ? std::string("admin") : std::string("internal"));
			 }});
		return [callbacks = std::move(callbacks),
				fallback = std::move(fallback)](hical::HttpRequest& request) -> std::optional<hical::HttpResponse>
		{
			const auto path = request.path();
			if (invalidPath(path))
			{
				request.native().keepAlive = false;
				auto result = hical::HttpResponse::json(
					boost::json::object {{"error", "请直接使用约定的路径，不要编码或添加特殊路径段"}});
				result.setStatus(hical::HttpStatusCode::hBadRequest);
				return result;
			}
			if (path == "/admin/health" || path == "/admin/ready")
			{
				return std::nullopt;
			}
			const auto found = callbacks.find(callbackGame(path));
			auto limited = found == callbacks.end() ? fallback(request) : found->second(request);
			if (limited)
			{
				// body 可能尚未消费；拒绝后关闭连接，防止残留字节被解释成下一条请求。
				request.native().keepAlive = false;
			}
			return limited;
		};
	}
} // namespace gamehub
