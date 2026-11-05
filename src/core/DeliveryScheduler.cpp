/** @file DeliveryScheduler.cpp
 * @brief 预占跨领取协程保留，领取完成后同步占用区服，避免相同目标超额投递。
 */
#include "DeliveryScheduler.h"
#include "Domain.h"
#include <algorithm>
#include <utility>

namespace gamehub
{
	DeliveryScheduler::DeliveryScheduler(const Config& config) : globalLimit_(config.worker.concurrency)
	{
		const auto selected = [&config](const GameConfig& game)
		{
			return config.worker.games.empty()
				   || std::find(config.worker.games.begin(), config.worker.games.end(), game.id)
						  != config.worker.games.end();
		};
		for (const auto& game : config.games)
		{
			enabledGames_ += selected(game) && game.deliveryEnabled;
		}
		const auto automaticLimit =
			std::max(1u, globalLimit_ / static_cast<unsigned>(std::max<std::size_t>(1, enabledGames_)));
		for (const auto& source : config.games)
		{
			Game game;
			game.id = source.id;
			game.selected = selected(source);
			game.enabled = game.selected && source.deliveryEnabled;
			game.limit = source.maxConcurrentDeliveries == 0 ? automaticLimit : source.maxConcurrentDeliveries;
			game.limit = std::min(game.limit, globalLimit_);
			game.targetLimit = source.maxConcurrentPerServer == 0 ? game.limit : source.maxConcurrentPerServer;
			game.targetLimit = std::min(game.targetLimit, game.limit);
			game.configuredLimit = source.maxConcurrentDeliveries;
			game.configuredTargetLimit = source.maxConcurrentPerServer;
			for (const auto& endpoint : source.servers)
			{
				game.targets.push_back(Target {endpoint.id, 0, {}});
			}
			games_.push_back(std::move(game));
		}
	}

	void DeliveryScheduler::refreshLimits()
	{
		enabledGames_ = static_cast<std::size_t>(std::count_if(games_.begin(),
															   games_.end(),
															   [](const Game& game)
															   {
																   return game.enabled;
															   }));
		const auto automaticLimit =
			std::max(1u, globalLimit_ / static_cast<unsigned>(std::max<std::size_t>(1, enabledGames_)));
		for (auto& game : games_)
		{
			game.limit = std::min(game.configuredLimit == 0 ? automaticLimit : game.configuredLimit, globalLimit_);
			game.targetLimit =
				std::min(game.configuredTargetLimit == 0 ? game.limit : game.configuredTargetLimit, game.limit);
		}
	}

	void DeliveryScheduler::applyControl(std::string_view gameId,
										 bool enabled,
										 unsigned gameLimit,
										 unsigned targetLimit,
										 std::uint64_t revision)
	{
		for (auto& game : games_)
		{
			if (game.id != gameId || revision <= game.controlRevision)
			{
				continue;
			}
			game.enabled = game.selected && enabled;
			game.configuredLimit = gameLimit;
			game.configuredTargetLimit = targetLimit;
			game.controlRevision = revision;
			refreshLimits();
			return;
		}
	}

	std::optional<DeliveryScheduler::Reservation> DeliveryScheduler::reserve(const std::vector<std::string>& skipped)
	{
		if (inFlight_ >= globalLimit_)
		{
			return std::nullopt;
		}
		for (std::size_t count = 0; count < games_.size(); ++count)
		{
			const auto index = nextGame_;
			nextGame_ = (nextGame_ + 1) % games_.size();
			auto& game = games_[index];
			if (!game.enabled || game.claiming || game.inFlight >= game.limit
				|| std::find(skipped.begin(), skipped.end(), game.id) != skipped.end())
			{
				continue;
			}
			const bool targetAvailable = std::any_of(game.targets.begin(),
													 game.targets.end(),
													 [&game](const Target& target)
													 {
														 return target.inFlight < game.targetLimit;
													 });
			if (!targetAvailable)
			{
				continue;
			}
			game.claiming = true;
			++game.inFlight;
			++inFlight_;
			return Reservation(*this, index);
		}
		return std::nullopt;
	}

	std::size_t DeliveryScheduler::enabledGames() const
	{
		return enabledGames_;
	}

	DeliveryScheduler::Reservation::Reservation(DeliveryScheduler& scheduler, std::size_t gameIndex)
		: scheduler_(&scheduler), gameIndex_(gameIndex), admittedTargetLimit_(scheduler.games_[gameIndex].targetLimit)
	{
	}

	DeliveryScheduler::Reservation::~Reservation()
	{
		release(DeliveryOutcome::Interrupted);
	}

	DeliveryScheduler::Reservation::Reservation(Reservation&& other) noexcept
		: scheduler_(std::exchange(other.scheduler_, nullptr))
		, gameIndex_(other.gameIndex_)
		, serverId_(other.serverId_)
		, admittedTargetLimit_(other.admittedTargetLimit_)
	{
	}

	DeliveryScheduler::Reservation& DeliveryScheduler::Reservation::operator=(Reservation&& other) noexcept
	{
		if (this != &other)
		{
			release(DeliveryOutcome::Interrupted);
			scheduler_ = std::exchange(other.scheduler_, nullptr);
			gameIndex_ = other.gameIndex_;
			serverId_ = other.serverId_;
			admittedTargetLimit_ = other.admittedTargetLimit_;
		}
		return *this;
	}

	std::string_view DeliveryScheduler::Reservation::gameId() const
	{
		return scheduler_->games_[gameIndex_].id;
	}

	std::vector<std::uint32_t> DeliveryScheduler::Reservation::excludedServers() const
	{
		std::vector<std::uint32_t> result;
		const auto& game = scheduler_->games_[gameIndex_];
		for (const auto& target : game.targets)
		{
			if (target.inFlight >= game.targetLimit)
			{
				result.push_back(target.id);
			}
		}
		return result;
	}

	DeliveryScheduler::Target& DeliveryScheduler::target(Game& game, std::uint32_t serverId)
	{
		const auto found = std::find_if(game.targets.begin(),
										game.targets.end(),
										[serverId](const Target& target)
										{
											return target.id == serverId;
										});
		if (found == game.targets.end())
		{
			throw ApiError(404, "领取的任务目标未配置，保留原业务身份处理旧服路由");
		}
		return *found;
	}

	void DeliveryScheduler::Reservation::attach(std::uint32_t serverId)
	{
		if (!scheduler_ || serverId_)
		{
			throw std::logic_error("投递名额已经释放或已登记目标");
		}
		auto& game = scheduler_->games_[gameIndex_];
		auto& target = scheduler_->target(game, serverId);
		// 领取期间允许刷新配额；这次预占按领取前的目标限额准入，后续 reserve 使用新限额。
		if (target.inFlight >= admittedTargetLimit_)
		{
			throw std::logic_error("任务领取没有排除已满额目标");
		}
		++target.inFlight;
		++target.counters.claimed;
		++game.counters.claimed;
		serverId_ = serverId;
		game.claiming = false;
	}

	void DeliveryScheduler::Reservation::complete(DeliveryOutcome outcome) noexcept
	{
		release(outcome);
	}

	void DeliveryScheduler::Reservation::release(DeliveryOutcome outcome) noexcept
	{
		if (!scheduler_)
		{
			return;
		}
		auto& game = scheduler_->games_[gameIndex_];
		if (serverId_)
		{
			// attach 后游戏目标列表不再修改；释放不分配、不抛异常，适用于取消和协程帧销毁。
			const auto found = std::find_if(game.targets.begin(),
											game.targets.end(),
											[this](const Target& target)
											{
												return target.id == *serverId_;
											});
			--found->inFlight;
			countOutcome(found->counters, outcome);
			countOutcome(game.counters, outcome);
		}
		else
		{
			game.claiming = false;
		}
		--game.inFlight;
		--scheduler_->inFlight_;
		scheduler_ = nullptr;
	}

	void DeliveryScheduler::countOutcome(Counters& counters, DeliveryOutcome outcome) noexcept
	{
		if (outcome == DeliveryOutcome::Success)
		{
			++counters.succeeded;
			return;
		}
		++counters.failed;
		switch (outcome)
		{
			case DeliveryOutcome::NetworkError:
				++counters.networkErrors;
				break;
			case DeliveryOutcome::Timeout:
				++counters.timeouts;
				++counters.networkErrors;
				break;
			case DeliveryOutcome::InvalidAck:
				++counters.invalidAcks;
				break;
			case DeliveryOutcome::ResponseTooLarge:
				++counters.responseTooLarge;
				break;
			case DeliveryOutcome::HttpError:
				++counters.httpErrors;
				break;
			case DeliveryOutcome::Exception:
				++counters.exceptions;
				break;
			case DeliveryOutcome::StorageError:
				++counters.storageErrors;
				break;
			case DeliveryOutcome::LeaseLost:
				++counters.leaseLost;
				break;
			case DeliveryOutcome::Interrupted:
				++counters.interrupted;
				break;
			case DeliveryOutcome::Success:
				break;
		}
	}

	boost::json::object DeliveryScheduler::countersJson(const Counters& counters)
	{
		return {{"claimed", counters.claimed},
				{"succeeded", counters.succeeded},
				{"failed", counters.failed},
				{"networkErrors", counters.networkErrors},
				{"timeouts", counters.timeouts},
				{"invalidAcks", counters.invalidAcks},
				{"responseTooLarge", counters.responseTooLarge},
				{"httpErrors", counters.httpErrors},
				{"exceptions", counters.exceptions},
				{"storageErrors", counters.storageErrors},
				{"leaseLost", counters.leaseLost},
				{"interrupted", counters.interrupted}};
	}

	boost::json::object DeliveryScheduler::gameJson(const Game& game) const
	{
		auto result = countersJson(game.counters);
		result["gameId"] = game.id;
		result["selected"] = game.selected;
		result["enabled"] = game.enabled;
		result["claiming"] = game.claiming;
		result["inFlight"] = game.inFlight;
		result["maxConcurrentDeliveries"] = game.limit;
		result["maxConcurrentPerServer"] = game.targetLimit;
		boost::json::array targets;
		for (const auto& target : game.targets)
		{
			auto item = countersJson(target.counters);
			item["serverId"] = target.id;
			item["inFlight"] = target.inFlight;
			targets.push_back(std::move(item));
		}
		result["targets"] = std::move(targets);
		result["controlRevision"] = std::to_string(game.controlRevision);
		return result;
	}

	boost::json::object DeliveryScheduler::snapshot(std::string_view gameId) const
	{
		if (!gameId.empty())
		{
			const auto found = std::find_if(games_.begin(),
											games_.end(),
											[gameId](const Game& game)
											{
												return game.id == gameId;
											});
			if (found == games_.end())
			{
				throw ApiError(404, "游戏不存在");
			}
			return gameJson(*found);
		}
		boost::json::array games;
		for (const auto& game : games_)
		{
			games.push_back(gameJson(game));
		}
		return {{"scope", "process"},
				{"inFlight", inFlight_},
				{"maxConcurrentDeliveries", globalLimit_},
				{"games", std::move(games)}};
	}
} // namespace gamehub
