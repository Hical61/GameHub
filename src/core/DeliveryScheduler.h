/** @file DeliveryScheduler.h
 * @brief 单事件循环中的游戏公平调度、领取串行化和目标并发配额。
 */
#pragma once
#include "Config.h"
#include <boost/json.hpp>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace gamehub
{
	/// 一次已领取任务在本进程中的最终结果，不替代持久化任务状态。
	enum class DeliveryOutcome
	{
		Success,
		NetworkError,
		Timeout,
		InvalidAck,
		ResponseTooLarge,
		HttpError,
		Exception,
		StorageError,
		LeaseLost,
		Interrupted
	};

	class DeliveryScheduler
	{
	public:
		class Reservation
		{
		public:
			/// @brief 释放本次预占名额；已领取但未完成时计为中断。无返回值。
			~Reservation();
			/// @param other 被转移的唯一预占名额；移动后原对象不再释放。无返回值。
			Reservation(Reservation&& other) noexcept;
			/// @param other 被转移的唯一预占名额；先释放本对象原名额。@return 当前对象。
			Reservation& operator=(Reservation&& other) noexcept;
			Reservation(const Reservation&) = delete;
			Reservation& operator=(const Reservation&) = delete;
			/// @return 本次领取限定的游戏；视图不得超过预占对象和调度器的生命周期。
			std::string_view gameId() const;
			/// @return 领取 SQL 必须排除的已满额目标；只在 attach 前调用。
			std::vector<std::uint32_t> excludedServers() const;
			/// @param serverId 已领取的目标；同步登记目标占用并允许该游戏下一笔领取。无返回值。
			void attach(std::uint32_t serverId);
			/// @param outcome 已领取任务的最终结果；计数一次并释放所有名额。无返回值。
			void complete(DeliveryOutcome outcome) noexcept;

		private:
			friend class DeliveryScheduler;
			Reservation(DeliveryScheduler& scheduler, std::size_t gameIndex);
			void release(DeliveryOutcome outcome) noexcept;
			DeliveryScheduler* scheduler_ = nullptr;
			std::size_t gameIndex_ = 0;
			std::optional<std::uint32_t> serverId_;
			unsigned admittedTargetLimit_ = 0;
		};

		/// @param config 启动后只读配置；预占对象必须比调度器先销毁，所有方法在同一事件循环调用。
		explicit DeliveryScheduler(const Config& config);
		/// @param skipped 当前 tick 已检查的游戏，避免反复查询空队列。
		/// @return 按轮转顺序预占的名额；全部暂停、满额或已检查时返回空。领取前已计入游戏/全局配额。
		std::optional<Reservation> reserve(const std::vector<std::string>& skipped = {});
		/// @return 本进程负责且允许投递的游戏数量。
		std::size_t enabledGames() const;
		/// @param gameId 非空时仅返回该游戏；未知游戏抛 404。@return 本进程配额、在途量及结果计数。
		boost::json::object snapshot(std::string_view gameId = {}) const;
		/// @param gameId 已配置游戏；enabled 是否领取；gameLimit/targetLimit 为0时自动分配；revision 控制版本。
		/// @return 无；降低配额只阻止新领取，已有预占仍正常释放；旧版本不覆盖新版本。
		void applyControl(std::string_view gameId,
						  bool enabled,
						  unsigned gameLimit,
						  unsigned targetLimit,
						  std::uint64_t revision);

	private:
		struct Counters
		{
			std::uint64_t claimed = 0;
			std::uint64_t succeeded = 0;
			std::uint64_t failed = 0;
			std::uint64_t networkErrors = 0;
			std::uint64_t timeouts = 0;
			std::uint64_t invalidAcks = 0;
			std::uint64_t responseTooLarge = 0;
			std::uint64_t httpErrors = 0;
			std::uint64_t exceptions = 0;
			std::uint64_t storageErrors = 0;
			std::uint64_t leaseLost = 0;
			std::uint64_t interrupted = 0;
		};

		struct Target
		{
			std::uint32_t id = 0;
			unsigned inFlight = 0;
			Counters counters;
		};

		struct Game
		{
			std::string id;
			bool enabled = false;
			bool selected = false;
			bool claiming = false;
			unsigned limit = 0;
			unsigned targetLimit = 0;
			unsigned inFlight = 0;
			Counters counters;
			std::vector<Target> targets;
			unsigned configuredLimit = 0;
			unsigned configuredTargetLimit = 0;
			std::uint64_t controlRevision = 0;
		};

		static void countOutcome(Counters& counters, DeliveryOutcome outcome) noexcept;
		static boost::json::object countersJson(const Counters& counters);
		boost::json::object gameJson(const Game& game) const;
		Target& target(Game& game, std::uint32_t serverId);
		void refreshLimits();
		std::vector<Game> games_;
		std::size_t nextGame_ = 0;
		std::size_t enabledGames_ = 0;
		unsigned globalLimit_ = 0;
		unsigned inFlight_ = 0;
	};
} // namespace gamehub
