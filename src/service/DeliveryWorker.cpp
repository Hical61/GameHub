/** @file DeliveryWorker.cpp
 * @brief 收到游戏服的业务确认才算完成，网络出错时允许重复投递。
 */
#include "DeliveryWorker.h"
#include "core/Crypto.h"
#include <chrono>
#include <core/Log.h>

namespace gamehub
{
	DeliveryWorker::DeliveryWorker(const Config& config,
								   const GameRegistry& registry,
								   Store& store,
								   hical::HttpClientPool& http)
		: config_(config), registry_(registry), store_(store), http_(http)
	{
	}

	Awaitable<void> DeliveryWorker::tick()
	{
		auto claimed = co_await store_.claim(config_.worker);
		if (!claimed)
		{
			co_return;
		}
		const auto& task = *claimed;
		bool success = false;
		std::string error = "delivery_exception";
		try
		{
			const auto& game = config_.game(task.gameId);
			const auto& endpoint = game.endpoint(task.serverId);
			const auto timestamp = std::to_string(unixSeconds());
			hical::HeaderMap headers;
			headers.set("Content-Type", "application/json");
			headers.set("X-Timestamp", timestamp);
			headers.set("X-Signature",
						registry_.find(task.gameId)
							.signOutbound(SignedRequest {timestamp, {}, "POST", endpoint.target, task.payload}));
			hical::HttpClientOptions options;
			options.connectTimeout = std::chrono::milliseconds(config_.worker.connectTimeoutMs);
			options.requestTimeout = std::chrono::milliseconds(config_.worker.requestTimeoutMs);
			options.maxResponseBodyBytes = config_.worker.maxResponseBodyBytes;
			const auto result = co_await http_.request(endpoint.host,
													   endpoint.port,
													   hical::HttpMethod::hPost,
													   endpoint.target,
													   task.payload,
													   headers,
													   options);
			error = result.error.code == hical::ErrorCode::hResponseBodyTooLarge
						? "response_too_large"
						: (result.isNetworkError() ? "network_error" : "http_" + std::to_string(result.status));
			if (result.status == 200 && !result.isNetworkError())
			{
				boost::system::error_code parseError;
				auto ack = boost::json::parse(result.body, parseError);
				success = !parseError && ack.is_object() && ack.as_object().if_contains("taskId")
						  && ack.as_object().at("taskId").is_string()
						  && ack.as_object().at("taskId").as_string() == std::to_string(task.id)
						  && ack.as_object().if_contains("committed") && ack.as_object().at("committed").is_bool()
						  && ack.as_object().at("committed").as_bool();
				error = success ? "" : "invalid_ack";
			}
		}
		catch (const std::exception&)
		{
			// 这里只记错误类别，不记录对方响应、SQL 参数或玩家数据。
			error = "delivery_exception";
		}
		const auto delay = retryDelay(task.attempts,
									  config_.worker.baseRetrySeconds,
									  config_.worker.maxRetrySeconds,
									  task.id ^ static_cast<std::uint64_t>(unixSeconds()));
		const bool updated = co_await store_.finish(task, success, error, delay, config_.worker.maxAttempts);
		if (!updated)
		{
			HICAL_LOG_WARN("收到投递结果时租约已失效，跳过更新，taskId={}", task.id);
		}
		else if (!success)
		{
			HICAL_LOG_WARN("投递失败，taskId={} attempts={} category={}", task.id, task.attempts, error);
		}
	}
} // namespace gamehub
