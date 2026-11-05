/** @file DeliveryWorker.h
 * @brief 在同一个事件循环中按槽位并发投递。
 */
#pragma once
#include "core/DeliveryScheduler.h"
#include "core/GameAdapter.h"
#include "core/Observability.h"
#include "storage/Store.h"
#include <asio/HttpClientPool.h>
#include <map>

namespace gamehub
{
	class DeliveryWorker
	{
	public:
		/// @param config 只读配置；registry 游戏注册表；store 存储；http 同一事件循环中的 HTTP 连接池。
		DeliveryWorker(const Config& config, const GameRegistry& registry, Store& store, hical::HttpClientPool& http);
		/// @return 是否领取并处理了任务；false时调用方按空闲轮询退避，同一槽位不得重入。
		Awaitable<bool> tick();
		/// @param gameId 非空时只读取该游戏；未知游戏抛 404。@return 本进程运行配额、在途和结果计数。
		boost::json::object snapshot(std::string_view gameId = {}) const;
		/// @param gameId 游戏；enabled 是否领取；gameLimit/targetLimit 进程内配额；revision 共享控制版本。@return 无。
		void applyControl(std::string_view gameId,
						  bool enabled,
						  unsigned gameLimit,
						  unsigned targetLimit,
						  std::uint64_t revision);
		/// @return 当前进程按游戏区服的投递耗时与成功完成延迟直方图，标签仅来自配置。
		std::string latencyMetrics() const;

	private:
		struct DeliveryResult
		{
			bool success = false;
			std::string error = "delivery_exception";
			DeliveryOutcome outcome = DeliveryOutcome::Exception;
		};

		Awaitable<DeliveryResult> deliver(const DeliveryTask& task);
		Awaitable<void> finish(const DeliveryTask& task,
							   DeliveryResult result,
							   DeliveryScheduler::Reservation& reservation);
		const Config& config_;
		const GameRegistry& registry_;
		Store& store_;
		hical::HttpClientPool& http_;
		DeliveryScheduler scheduler_;

		struct Latencies
		{
			LatencyHistogram delivery;
			LatencyHistogram completed;
		};

		std::map<std::pair<std::string, std::uint32_t>, Latencies> latencies_;
	};
} // namespace gamehub
