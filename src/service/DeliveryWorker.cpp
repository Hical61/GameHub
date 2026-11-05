/** @file DeliveryWorker.cpp
 * @brief 按游戏公平领取，发送 HTTP 时仅占投递配额；业务提交确认后才完成任务。
 */
#include "DeliveryWorker.h"
#include "core/DeliveryProtocol.h"
#include <algorithm>
#include <boost/asio/error.hpp>
#include <boost/system/system_error.hpp>
#include <chrono>
#include <core/Log.h>

namespace gamehub
{
	namespace
	{
		// 性能敏感：限制一次空闲轮询的数据库往返，不随配置游戏总数线性增长。
		constexpr std::size_t kMaxGameChecksPerTick = 4;

		bool validAck(std::string_view body, std::uint64_t taskId)
		{
			boost::system::error_code error;
			const auto value = boost::json::parse(body, error);
			if (error || !value.is_object())
			{
				return false;
			}
			const auto* id = value.as_object().if_contains("taskId");
			const auto* committed = value.as_object().if_contains("committed");
			return id && id->is_string() && id->as_string() == std::to_string(taskId) && committed
				   && committed->is_bool() && committed->as_bool();
		}
	} // namespace

	DeliveryWorker::DeliveryWorker(const Config& config,
								   const GameRegistry& registry,
								   Store& store,
								   hical::HttpClientPool& http)
		: config_(config), registry_(registry), store_(store), http_(http), scheduler_(config)
	{
	}

	Awaitable<bool> DeliveryWorker::tick()
	{
		std::vector<std::string> checked;
		const auto budget = std::min(kMaxGameChecksPerTick, scheduler_.enabledGames());
		for (std::size_t count = 0; count < budget; ++count)
		{
			auto reservation = scheduler_.reserve(checked);
			if (!reservation)
			{
				co_return false;
			}
			checked.emplace_back(reservation->gameId());
			// 预占已计入游戏配额，同游戏领取串行化；目标登记后下一笔领取才能看到满额目标。
			auto claimed = co_await store_.claim(config_.worker, checked.back(), reservation->excludedServers());
			if (!claimed)
			{
				continue;
			}
			DeliveryResult result;
			const auto started = std::chrono::steady_clock::now();
			bool targetRegistered = false;
			try
			{
				reservation->attach(claimed->serverId);
				targetRegistered = true;
				result = co_await deliver(*claimed);
			}
			catch (const boost::system::system_error& error)
			{
				if (error.code() == boost::asio::error::operation_aborted)
				{
					throw;
				}
			}
			catch (const std::exception&)
			{
				// 固定分类不包含 SQL 参数、玩家字段或第三方响应。
			}
			if (targetRegistered)
			{
				latencies_[{claimed->gameId, claimed->serverId}].delivery.observe(
					std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count());
			}
			co_await finish(*claimed, std::move(result), *reservation);
			co_return true;
		}
		co_return false;
	}

	Awaitable<DeliveryWorker::DeliveryResult> DeliveryWorker::deliver(const DeliveryTask& task)
	{
		const auto& game = config_.game(task.gameId);
		const auto& endpoint = game.endpoint(task.serverId);
		const auto timestamp = std::to_string(unixSeconds());
		const auto body = encodeDelivery(task);
		hical::HeaderMap headers;
		headers.set("Content-Type", "application/json");
		headers.set("X-Timestamp", timestamp);
		const auto keyId = registry_.find(task.gameId).outboundKeyId();
		if (!keyId.empty())
		{
			headers.set("X-Key-Id", std::string(keyId));
		}
		headers.set("X-Signature",
					registry_.find(task.gameId)
						.signOutbound(SignedRequest {timestamp, {}, "POST", endpoint.target, body, keyId}));
		hical::HttpClientOptions options;
		options.connectTimeout = std::chrono::milliseconds(config_.worker.connectTimeoutMs);
		options.requestTimeout = std::chrono::milliseconds(config_.worker.requestTimeoutMs);
		options.maxResponseBodyBytes = config_.worker.maxResponseBodyBytes;
		const auto result = co_await http_.request(endpoint.host,
												   endpoint.port,
												   hical::HttpMethod::hPost,
												   endpoint.target,
												   body,
												   headers,
												   options);
		if (result.error.code == hical::ErrorCode::hResponseBodyTooLarge)
		{
			co_return DeliveryResult {false, "response_too_large", DeliveryOutcome::ResponseTooLarge};
		}
		if (result.isNetworkError())
		{
			const bool timeout = result.error.code == hical::ErrorCode::hTimedOut;
			co_return DeliveryResult {false,
									  timeout ? "network_timeout" : "network_error",
									  timeout ? DeliveryOutcome::Timeout : DeliveryOutcome::NetworkError};
		}
		if (result.status != 200)
		{
			co_return DeliveryResult {false, "http_" + std::to_string(result.status), DeliveryOutcome::HttpError};
		}
		if (!validAck(result.body, task.id))
		{
			co_return DeliveryResult {false, "invalid_ack", DeliveryOutcome::InvalidAck};
		}
		co_return DeliveryResult {true, {}, DeliveryOutcome::Success};
	}

	Awaitable<void> DeliveryWorker::finish(const DeliveryTask& task,
										   DeliveryResult result,
										   DeliveryScheduler::Reservation& reservation)
	{
		const auto delay = retryDelay(task.attempts,
									  config_.worker.baseRetrySeconds,
									  config_.worker.maxRetrySeconds,
									  task.id ^ static_cast<std::uint64_t>(unixSeconds()));
		bool updated = false;
		try
		{
			updated = co_await store_.finish(task, result.success, result.error, delay, config_.worker.maxAttempts);
		}
		catch (...)
		{
			reservation.complete(DeliveryOutcome::StorageError);
			throw;
		}
		reservation.complete(updated ? result.outcome : DeliveryOutcome::LeaseLost);
		if (updated && result.success && task.createdAtUnixMicros > 0)
		{
			const auto now = std::chrono::duration_cast<std::chrono::microseconds>(
								 std::chrono::system_clock::now().time_since_epoch())
								 .count();
			latencies_[{task.gameId, task.serverId}].completed.observe(
				static_cast<double>(std::max<std::int64_t>(0, now - task.createdAtUnixMicros)) / 1'000'000.0);
		}
		if (!updated)
		{
			HICAL_LOG_WARN("收到投递结果时租约已失效，跳过更新，taskId={}", task.id);
		}
		else if (!result.success)
		{
			HICAL_LOG_WARN("投递失败，taskId={} attempts={} category={}", task.id, task.attempts, result.error);
		}
	}

	boost::json::object DeliveryWorker::snapshot(std::string_view gameId) const
	{
		return scheduler_.snapshot(gameId);
	}

	void DeliveryWorker::applyControl(std::string_view gameId,
									  bool enabled,
									  unsigned gameLimit,
									  unsigned targetLimit,
									  std::uint64_t revision)
	{
		scheduler_.applyControl(gameId, enabled, gameLimit, targetLimit, revision);
	}

	std::string DeliveryWorker::latencyMetrics() const
	{
		std::string output;
		for (const auto& [identity, metrics] : latencies_)
		{
			const auto labels = "game=\"" + identity.first + "\",server=\"" + std::to_string(identity.second) + "\"";
			output += metrics.delivery.prometheus("gamehub_delivery_seconds", labels);
			output += metrics.completed.prometheus("gamehub_completion_seconds", labels);
		}
		return output;
	}
} // namespace gamehub
