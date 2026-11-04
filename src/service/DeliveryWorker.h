/** @file DeliveryWorker.h
 * @brief 在同一个事件循环中按槽位并发投递。
 */
#pragma once
#include "core/DeliveryScheduler.h"
#include "core/GameAdapter.h"
#include "storage/Store.h"
#include <asio/HttpClientPool.h>

namespace gamehub
{
	class DeliveryWorker
	{
	public:
		/// @param config 只读配置；registry 游戏注册表；store 存储；http 同一事件循环中的 HTTP 连接池。
		DeliveryWorker(const Config& config, const GameRegistry& registry, Store& store, hical::HttpClientPool& http);
		/// @return 本次投递完成；每次最多领取一条任务，调用者不能同时启动同一槽位。
		Awaitable<void> tick();
		/// @param gameId 非空时只读取该游戏；未知游戏抛 404。@return 本进程运行配额、在途和结果计数。
		boost::json::object snapshot(std::string_view gameId = {}) const;

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
	};
} // namespace gamehub
