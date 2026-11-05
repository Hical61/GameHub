/** @file ApplicationOperations.cpp
 * @brief 有游戏授权的查单、尝试历史、共享控制和配置应用回报。
 */
#include "Application.h"
#include "core/Crypto.h"
#include <charconv>
#include <limits>
#include <unordered_set>

namespace gamehub
{
	namespace
	{
		hical::HttpResponse jsonReply(const boost::json::value& body)
		{
			auto result = hical::HttpResponse::json(body);
			result.setHeader("Cache-Control", "no-store");
			return result;
		}

		std::uint64_t number(std::string_view text, std::uint64_t maximum = INT64_MAX, bool zero = true)
		{
			std::uint64_t value = 0;
			const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
			if (text.empty() || error != std::errc {} || end != text.data() + text.size() || value > maximum
				|| (!zero && value == 0))
			{
				throw ApiError(400, "数值参数无效");
			}
			return value;
		}

		TaskFilter taskFilter(const hical::HttpRequest& request)
		{
			TaskFilter filter;
			filter.after = number(request.param("after"));
			std::unordered_set<std::string> seen;
			for (const auto& [key, value] : request.queryParams())
			{
				if (!seen.insert(key).second)
				{
					throw ApiError(400, "筛选字段不能重复");
				}
				if (key == "status")
				{
					filter.status = static_cast<TaskStatus>(number(value, 3));
				}
				else if (key == "serverId")
				{
					filter.serverId = static_cast<std::uint32_t>(number(value, UINT32_MAX, false));
				}
				else if (key == "createdFrom")
				{
					filter.createdFrom = static_cast<std::int64_t>(number(value));
				}
				else if (key == "createdBefore")
				{
					filter.createdBefore = static_cast<std::int64_t>(number(value));
				}
				else if (key == "limit")
				{
					filter.limit = static_cast<unsigned>(number(value, 100, false));
				}
				else
				{
					throw ApiError(400, "未知任务筛选字段");
				}
			}
			if (filter.createdFrom && filter.createdBefore && *filter.createdFrom >= *filter.createdBefore)
			{
				throw ApiError(400, "创建时间范围无效");
			}
			return filter;
		}

		boost::json::object controlJson(const DeliveryControl& control, bool persisted)
		{
			return {{"gameId", control.gameId},
					{"revision", std::to_string(control.version)},
					{"deliveryEnabled", control.enabled},
					{"maxConcurrentDeliveries", control.maxConcurrentDeliveries},
					{"maxConcurrentPerServer", control.maxConcurrentPerServer},
					{"scope", persisted ? "shared" : "startup"}};
		}

		boost::json::object page(boost::json::array items, std::string_view cursor)
		{
			boost::json::value next = items.empty() ? boost::json::value("0") : items.back().as_object().at(cursor);
			return {{"items", std::move(items)}, {"nextAfter", std::move(next)}};
		}
	} // namespace

	Awaitable<hical::HttpResponse> Application::operationsRead(const hical::HttpRequest& request)
	{
		const auto& game = config_.game(request.param("game"));
		const auto path = request.path();
		if (path.starts_with("/admin/events/"))
		{
			if (!validKey(request.param("type"), 32) || !validKey(request.param("namespace"), 32)
				|| !validKey(request.param("key")))
			{
				throw ApiError(400, "业务身份无效");
			}
			co_return jsonReply(co_await store_.lookupEvent(game.id,
															request.param("type"),
															request.param("namespace"),
															request.param("key")));
		}
		if (path.starts_with("/admin/attempts/"))
		{
			co_return jsonReply(page(co_await store_.listAttempts(game.id,
																  number(request.param("id"), INT64_MAX, false),
																  number(request.param("after"))),
									 "id"));
		}
		if (path.starts_with("/admin/config-status/"))
		{
			co_return jsonReply(page(co_await store_.listConfigStates(
										 game.id,
										 static_cast<std::uint32_t>(number(request.param("after"), UINT32_MAX))),
									 "serverId"));
		}
		if (path.starts_with("/admin/tasks/"))
		{
			co_return jsonReply(page(co_await store_.listTasks(game.id, taskFilter(request)), "id"));
		}
		auto control = co_await store_.readDeliveryControl(game.id);
		if (!control)
		{
			control = DeliveryControl {game.id,
									   0,
									   game.deliveryEnabled,
									   game.maxConcurrentDeliveries,
									   game.maxConcurrentPerServer};
		}
		co_return jsonReply(controlJson(*control, control->version > 0));
	}

	Awaitable<hical::HttpResponse> Application::controlWrite(const hical::HttpRequest& request, std::string actor)
	{
		const auto& game = config_.game(request.param("game"));
		const auto body = parseObject(request.body());
		const auto* enabled = body.if_contains("deliveryEnabled");
		if (body.size() < 2 || body.size() > 4 || (enabled && !enabled->is_bool()))
		{
			throw ApiError(400, "控制更新需要期望版本、投递开关和游戏及区服配额");
		}
		for (const auto& field : body)
		{
			if (field.key() != "expectedRevision" && field.key() != "deliveryEnabled"
				&& field.key() != "maxConcurrentDeliveries" && field.key() != "maxConcurrentPerServer")
			{
				throw ApiError(400, "未知投递控制字段");
			}
		}
		auto previous = co_await store_.readDeliveryControl(game.id);
		DeliveryControl desired = previous.value_or(DeliveryControl {game.id,
																	 0,
																	 game.deliveryEnabled,
																	 game.maxConcurrentDeliveries,
																	 game.maxConcurrentPerServer});
		if (enabled)
		{
			desired.enabled = enabled->as_bool();
		}
		if (body.if_contains("maxConcurrentDeliveries"))
		{
			desired.maxConcurrentDeliveries =
				static_cast<unsigned>(requiredInt(body, "maxConcurrentDeliveries", 0, 32));
		}
		if (body.if_contains("maxConcurrentPerServer"))
		{
			desired.maxConcurrentPerServer = static_cast<unsigned>(requiredInt(body, "maxConcurrentPerServer", 0, 32));
		}
		const auto expected = static_cast<std::uint64_t>(requiredInt(body, "expectedRevision", 0, INT64_MAX - 1));
		const auto saved = co_await store_.updateDeliveryControl(std::move(desired), expected, std::move(actor));
		worker_.applyControl(saved.gameId,
							 saved.enabled,
							 saved.maxConcurrentDeliveries,
							 saved.maxConcurrentPerServer,
							 saved.version);
		co_return jsonReply(controlJson(saved, true));
	}

	Awaitable<hical::HttpResponse> Application::configApplied(const hical::HttpRequest& request)
	{
		const auto& game = config_.game(request.param("game"));
		const auto server = static_cast<std::uint32_t>(number(request.param("server"), UINT32_MAX, false));
		game.endpoint(server);
		const SignedRequest signedView {request.header("X-Timestamp"),
										request.header("X-Signature"),
										"POST",
										request.path(),
										request.body(),
										request.header("X-Key-Id")};
		if (!registry_.find(game.id).verifyInternalRequest(signedView, unixSeconds()))
		{
			throw ApiError(401, "签名无效或请求已过期");
		}
		const auto body = parseObject(request.body());
		if (body.size() != 2)
		{
			throw ApiError(400, "应用回报只能包含版本和内容摘要");
		}
		const auto version = static_cast<std::uint64_t>(requiredInt(body, "version", 1, INT64_MAX));
		co_return jsonReply(
			co_await store_.reportConfigApplied(game.id, server, version, requiredString(body, "contentHash", 64)));
	}
} // namespace gamehub
