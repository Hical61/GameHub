/** @file ApplicationTasks.cpp
 * @brief 周期恢复、共享控制刷新与有界连续投递。
 */
#include "Application.h"
#include <core/Log.h>
#include <boost/system/system_error.hpp>

namespace gamehub
{
	namespace
	{
		constexpr auto kDeliveryWakeInterval = std::chrono::milliseconds(10);
		constexpr auto kDeliveryTimeSlice = std::chrono::milliseconds(100);
		constexpr auto kMonitorInterval = std::chrono::seconds(30);
		constexpr auto kRecoveryInterval = std::chrono::seconds(1);
		constexpr unsigned kBacklogAlertSeconds = 60;
	} // namespace

	Awaitable<void> Application::refreshControl()
	{
		std::vector<std::string> games;
		for (const auto& game : config_.games)
		{
			games.push_back(game.id);
		}
		for (const auto& control : co_await store_.readDeliveryControls(std::move(games)))
		{
			worker_.applyControl(control.gameId,
								 control.enabled,
								 control.maxConcurrentDeliveries,
								 control.maxConcurrentPerServer,
								 control.version);
		}
	}

	Awaitable<void> Application::recoverAndRefresh()
	{
		try
		{
			if (!schemaReady_ || std::chrono::steady_clock::now() >= nextSchemaCheck_)
			{
				co_await store_.checkSchema();
				nextSchemaCheck_ = std::chrono::steady_clock::now() + kMonitorInterval;
			}
			co_await refreshControl();
			const auto count = co_await store_.recover(config_.worker, recoveryGames_);
			recovered_ += count;
			schemaReady_ = !stopping_;
			if (count > 0)
			{
				HICAL_LOG_WARN("已回收过期的投递租约，count={}", count);
			}
		}
		catch (const boost::system::system_error& error)
		{
			schemaReady_ = false;
			HICAL_LOG_ERROR("数据库、共享控制或迁移检查失败，category={} code={}",
							error.code().category().name(),
							error.code().value());
		}
		catch (const std::exception&)
		{
			schemaReady_ = false;
			HICAL_LOG_ERROR("数据库、共享控制或迁移检查失败，暂停新业务及任务领取");
		}
	}

	void Application::registerTasks()
	{
		const auto weak = weak_from_this();
		server_.addPeriodicTask("schema-and-recovery",
								kRecoveryInterval,
								[weak]() -> Awaitable<void>
								{
									if (auto self = weak.lock(); self && !self->stopping_)
									{
										co_await self->recoverAndRefresh();
									}
								});
		registerDeliveryTasks();
		server_.addPeriodicTask("delivery-monitor",
								kMonitorInterval,
								[weak]() -> Awaitable<void>
								{
									auto self = weak.lock();
									if (!self || !self->schemaReady_ || self->stopping_)
									{
										co_return;
									}
									const auto stats = co_await self->store_.stats();
									if (stats.at("failed").to_number<std::uint64_t>() > 0
										|| stats.at("oldestPendingSeconds").to_number<std::uint64_t>()
											   > kBacklogAlertSeconds)
									{
										HICAL_LOG_ERROR("有任务积压或投递失败，请查看 /admin/stats");
									}
								});
	}

	void Application::registerDeliveryTasks()
	{
		for (unsigned slot = 0; slot < config_.worker.concurrency; ++slot)
		{
			auto nextPoll = std::make_shared<std::chrono::steady_clock::time_point>();
			server_.addPeriodicTask(
				"delivery-" + std::to_string(slot),
				kDeliveryWakeInterval,
				[weak = weak_from_this(), nextPoll]() -> Awaitable<void>
				{
					auto self = weak.lock();
					if (!self || !self->schemaReady_ || self->stopping_ || std::chrono::steady_clock::now() < *nextPoll)
					{
						co_return;
					}
					const auto deadline = std::chrono::steady_clock::now() + kDeliveryTimeSlice;
					try
					{
						// 性能敏感：忙时不等待pollMs；每轮有任务及时间预算，回到事件循环后继续公平轮转。
						for (unsigned count = 0; count < self->config_.worker.maxTasksPerTick; ++count)
						{
							if (!self->schemaReady_ || self->stopping_)
							{
								co_return;
							}
							if (!co_await self->worker_.tick())
							{
								*nextPoll = std::chrono::steady_clock::now()
											+ std::chrono::milliseconds(self->config_.worker.pollMs);
								co_return;
							}
							if (std::chrono::steady_clock::now() >= deadline)
							{
								break;
							}
						}
					}
					catch (const std::exception&)
					{
						*nextPoll =
							std::chrono::steady_clock::now() + std::chrono::milliseconds(self->config_.worker.pollMs);
						HICAL_LOG_ERROR("投递失败，未确认任务保留原身份等待租约回收");
					}
				});
		}
	}
} // namespace gamehub
