/** @file DeliveryWorker.h
 * @brief 在同一个事件循环中按槽位并发投递。
 */
#pragma once
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

	private:
		const Config& config_;
		const GameRegistry& registry_;
		Store& store_;
		hical::HttpClientPool& http_;
	};
} // namespace gamehub
