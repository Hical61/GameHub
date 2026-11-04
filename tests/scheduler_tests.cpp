/** @file scheduler_tests.cpp
 * @brief 验证多游戏领取时序、目标并发限制及取消/移动后的配额回收。
 */
#include "core/DeliveryScheduler.h"
#include "core/Domain.h"
#include <algorithm>
#include <iostream>
#include <stdexcept>

namespace
{
	unsigned failures = 0;

	void check(bool condition, const char* message)
	{
		if (!condition)
		{
			++failures;
			std::cerr << "失败：" << message << '\n';
		}
	}

	gamehub::Config configFor(unsigned games, unsigned slots = 4)
	{
		gamehub::Config config;
		config.worker.concurrency = slots;
		for (unsigned index = 0; index < games; ++index)
		{
			gamehub::GameConfig game;
			game.id = "game-" + std::to_string(index);
			game.maxConcurrentPerServer = 1;
			game.servers = {{1, "127.0.0.1", 9001, "/internal"}, {2, "127.0.0.1", 9002, "/internal"}};
			config.games.push_back(std::move(game));
		}
		return config;
	}

	std::uint64_t count(const boost::json::object& value, const char* key)
	{
		return value.at(key).to_number<std::uint64_t>();
	}

	void concurrentClaimsRespectTargets()
	{
		gamehub::DeliveryScheduler scheduler(configFor(2));
		auto first = scheduler.reserve();
		auto second = scheduler.reserve();
		check(first && first->gameId() == "game-0", "从第一款游戏开始轮转");
		check(second && second->gameId() == "game-1", "下一槽位领取另一款游戏");
		check(!scheduler.reserve(), "同游戏领取在途时不允许第二笔并行领取");
		first->attach(1);
		second->attach(1);
		auto third = scheduler.reserve();
		check(third && third->gameId() == "game-0", "已登记目标后允许继续领取同游戏");
		check(third->excludedServers() == std::vector<std::uint32_t> {1}, "领取查询排除已满额区服");
		third->attach(2);
		auto fourth = scheduler.reserve();
		check(fourth && fourth->gameId() == "game-1", "慢游戏占满自身配额后另一游戏仍可领取");
		fourth->attach(2);
		check(!scheduler.reserve(), "全局上限在多个游戏之间共同生效");
		first->complete(gamehub::DeliveryOutcome::Success);
		third->complete(gamehub::DeliveryOutcome::Timeout);
		second->complete(gamehub::DeliveryOutcome::InvalidAck);
		fourth->complete(gamehub::DeliveryOutcome::LeaseLost);
		const auto game = scheduler.snapshot("game-0");
		check(count(game, "claimed") == 2 && count(game, "succeeded") == 1 && count(game, "failed") == 1,
			  "同游戏的最终结果只统计一次");
		check(count(game, "networkErrors") == 1 && count(game, "timeouts") == 1, "超时包含在网络错误计数中");
		check(count(scheduler.snapshot(), "inFlight") == 0, "所有完成路径归还全局名额");
		check(!game.if_contains("games") && !game.if_contains("scope"), "游戏视图不包含其他游戏和全局负载");
	}

	void emptyQueuesAndScope()
	{
		auto config = configFor(3);
		config.worker.games = {"game-1", "game-2"};
		config.games[2].deliveryEnabled = false;
		gamehub::DeliveryScheduler scheduler(config);
		check(scheduler.enabledGames() == 1, "只调度本进程负责且启用的游戏");
		check(!scheduler.snapshot("game-0").at("selected").as_bool(), "显示游戏不在本进程领取范围");
		check(!scheduler.snapshot("game-2").at("enabled").as_bool(), "暂停游戏不被领取");
		std::vector<std::string> checked;
		{
			auto reservation = scheduler.reserve();
			check(reservation && reservation->gameId() == "game-1", "领取只命中指定游戏");
			checked.emplace_back(reservation->gameId());
		}
		check(!scheduler.reserve(checked), "空队列在同一 tick 不会被重复查询");
		check(scheduler.reserve().has_value(), "下一个 tick 可以再次检查原空队列");
		check(count(scheduler.snapshot("game-1"), "claimed") == 0, "没有领取到任务不计为失败或任务");
		check(count(scheduler.snapshot(), "inFlight") == 0, "未领取到任务自动释放预占");
		try
		{
			scheduler.snapshot("missing");
			check(false, "未知游戏应返回 404");
		}
		catch (const gamehub::ApiError& error)
		{
			check(error.status == 404, "未知游戏明确区分于空统计");
		}
	}

	void movesAndCancellationReleaseOnce()
	{
		auto config = configFor(1);
		gamehub::DeliveryScheduler scheduler(config);
		{
			auto source = scheduler.reserve();
			source->attach(1);
			auto moved = std::move(*source);
			source.reset();
			check(count(scheduler.snapshot(), "inFlight") == 1, "移动源析构不会提前释放名额");
			{
				auto replacement = scheduler.reserve();
				replacement->attach(2);
				moved = std::move(*replacement);
				check(count(scheduler.snapshot(), "inFlight") == 1, "移动赋值先释放原名额，再接管新名额");
			}
			check(count(scheduler.snapshot(), "inFlight") == 1, "被移动对象析构无重复释放");
			moved.complete(gamehub::DeliveryOutcome::Success);
			moved.complete(gamehub::DeliveryOutcome::Success);
		}
		check(count(scheduler.snapshot(), "inFlight") == 0, "显式完成和析构不会重复释放");
		{
			auto cancelled = scheduler.reserve();
			cancelled->attach(1);
		}
		const auto game = scheduler.snapshot("game-0");
		check(count(game, "claimed") == 3 && count(game, "interrupted") == 2 && count(game, "succeeded") == 1,
			  "取消和移动覆盖都记录中断，成功不重复计数");
		check(scheduler.reserve().has_value(), "中断后可重新领取，无配额泄漏");
	}

	void automaticBudgetAndFairness()
	{
		auto config = configFor(3, 6);
		gamehub::DeliveryScheduler scheduler(config);
		check(count(scheduler.snapshot("game-0"), "maxConcurrentDeliveries") == 2, "自动配额按启用游戏数划分");
		for (unsigned index = 0; index < 12; ++index)
		{
			auto reservation = scheduler.reserve();
			check(reservation && reservation->gameId() == "game-" + std::to_string(index % 3),
				  "持续有任务的游戏获得轮转机会");
			reservation->attach(1);
			reservation->complete(gamehub::DeliveryOutcome::Success);
		}
		gamehub::DeliveryScheduler single(configFor(1));
		check(count(single.snapshot("game-0"), "maxConcurrentDeliveries") == 4, "单游戏保持原全局并发能力");
		config.games[0].maxConcurrentDeliveries = 20;
		config.games[0].maxConcurrentPerServer = 30;
		gamehub::DeliveryScheduler bounded(config);
		check(count(bounded.snapshot("game-0"), "maxConcurrentDeliveries") == 6
				  && count(bounded.snapshot("game-0"), "maxConcurrentPerServer") == 6,
			  "显式游戏及区服配额不会超过全局并发");
	}

	void unavailableTargetAndInvalidAttach()
	{
		auto config = configFor(1);
		config.games[0].servers.resize(1);
		gamehub::DeliveryScheduler scheduler(config);
		{
			auto reservation = scheduler.reserve();
			reservation->attach(1);
			check(!scheduler.reserve(), "全部区服都满额时不执行无效领取查询");
		}
		{
			auto reservation = scheduler.reserve();
			try
			{
				reservation->attach(99);
				check(false, "未知旧服路由不能默默发送到其他区服");
			}
			catch (const gamehub::ApiError&)
			{
			}
		}
		check(count(scheduler.snapshot(), "inFlight") == 0, "目标登记失败也释放领取占用");
		check(scheduler.reserve().has_value(), "目标登记异常不阻塞后续领取");
	}
} // namespace

int main()
{
	concurrentClaimsRespectTargets();
	emptyQueuesAndScope();
	movesAndCancellationReleaseOnce();
	automaticBudgetAndFairness();
	unavailableTargetAndInvalidAttach();
	if (failures == 0)
	{
		std::cout << "调度与配额测试通过\n";
	}
	return failures == 0 ? 0 : 1;
}
