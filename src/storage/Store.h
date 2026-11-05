/** @file Store.h
 * @brief 保存事件、投递任务、配置版本和审计记录。
 */
#pragma once
#include "Database.h"
#include "core/Domain.h"
#include <optional>

namespace gamehub
{
	inline constexpr unsigned kMaxMaintenanceBatch = 1000;
	inline constexpr unsigned kMaxRetentionDays = 36500;
	inline constexpr unsigned kMinRetentionDays = 1;

	struct AcceptResult
	{
		std::uint64_t eventId = 0;
		bool duplicate = false;
	};

	struct TaskFilter
	{
		std::uint64_t after = 0;
		unsigned limit = 100;
		std::optional<TaskStatus> status;
		std::optional<std::uint32_t> serverId;
		// UTC Unix 秒；起始时间包含，结束时间不包含。
		std::optional<std::int64_t> createdFrom;
		std::optional<std::int64_t> createdBefore;
	};

	struct DeliveryControl
	{
		std::string gameId;
		std::uint64_t version = 0;
		bool enabled = true;
		unsigned maxConcurrentDeliveries = 0;
		unsigned maxConcurrentPerServer = 0;
	};

	class Store
	{
	public:
		/// @param database 数据库池，必须比 Store 活得更久。
		explicit Store(std::shared_ptr<Database> database) : database_(std::move(database))
		{
		}

		/// @return 检查迁移版本和核心表；检查失败时抛异常。
		Awaitable<void> checkSchema();
		/// @param submission 检查通过的业务数据。@return 同一事务内保存事件和任务的结果；同键不同内容时抛 409。
		Awaitable<AcceptResult> accept(Submission submission);
		/// @param config 租约和重试上限；兼容全局领取入口，应用投递必须使用带 gameId 的重载。
		/// @return 已提交领取事务的任务；没有到期任务，或选中的任务已用完重试次数、转为 failed 时返回空。
		Awaitable<std::optional<DeliveryTask>> claim(const WorkerConfig& config);
		/// @param config 租约和重试上限；gameId 必须为明确的游戏；excludedServers 是本进程已满额的目标。
		/// @return 仅属于该游戏且目标未被排除的已提交领取任务；无候选时返回空。
		Awaitable<std::optional<DeliveryTask>> claim(const WorkerConfig& config,
													 std::string gameId,
													 std::vector<std::uint32_t> excludedServers = {});
		/// @param config 重试上限。@return 全局回收过期任务数，最多 100 条；分游戏部署必须使用带范围的重载。
		Awaitable<std::uint64_t> recover(const WorkerConfig& config);
		/// @param config 重试上限；gameIds 为本进程负责的游戏范围，空范围不回收任何任务。
		/// @return 范围内已回收过期任务数。总额 100 按游戏均分，余数依列表顺序分配，空闲游戏的名额不借用。
		Awaitable<std::uint64_t> recover(const WorkerConfig& config, std::vector<std::string> gameIds);
		/// @param task 领取的任务；success 是否收到业务确认；error 固定错误类别；delay 重试间隔秒数；maxAttempts 重试上限。
		/// @return 是否持有有效租约并更新成功；旧代次或租约过期时不能更新。
		Awaitable<bool> finish(const DeliveryTask& task,
							   bool success,
							   std::string error,
							   unsigned delay,
							   unsigned maxAttempts);
		/// @param gameId 游戏标识；after ID 游标。@return 最多 100 条任务，不返回订单内容。
		Awaitable<boost::json::array> listTasks(std::string gameId, std::uint64_t after);
		/// @param gameId 游戏；filter 有界游标、状态、区服及 UTC 创建时间范围。@return 任务元信息，不包含业务载荷。
		Awaitable<boost::json::array> listTasks(std::string gameId, TaskFilter filter);
		/// @param gameId/taskType/nameSpace/bizKey 完整业务身份。@return 事件与活动/归档任务元信息；未找到抛 404。
		Awaitable<boost::json::object> lookupEvent(std::string gameId,
												   std::string taskType,
												   std::string nameSpace,
												   std::string bizKey);
		/// @param gameId 游戏；taskId 任务；after 尝试记录游标；limit 为 1 到 100。@return 无业务载荷的持久尝试历史。
		Awaitable<boost::json::array> listAttempts(std::string gameId,
												   std::uint64_t taskId,
												   std::uint64_t after = 0,
												   unsigned limit = 100);
		/// @param gameId 游戏标识；after ID 游标。@return 最多 100 条已归档任务的元信息，不返回载荷。
		Awaitable<boost::json::array> listArchived(std::string gameId, std::uint64_t after);
		/// @param gameId 游戏标识；taskId 任务编号；actor 操作人。@return 是否将 failed 任务重排；状态更新和审计写在同一事务中。
		Awaitable<bool> replay(std::string gameId, std::uint64_t taskId, std::string actor);
		/// @return 当前各状态、归档数量和最老 pending 的等待时间；只汇总固定数量的计数行。
		Awaitable<boost::json::object> stats();
		/// @param gameId 游戏标识。@return 该游戏的状态计数与最老积压，最多汇总 64 个计数分片。
		Awaitable<boost::json::object> stats(std::string gameId);
		/// @param gameIds 至多 100 款固定配置游戏，空数组不查询。@return 单次 SQL 汇总各游戏状态和最老积压，无任务时返回零值。
		Awaitable<boost::json::array> gameStats(std::vector<std::string> gameIds);
		/// @param gameId 游戏；keepDays 保留天数；batchSize 单批上限；actor 操作者。
		/// @return 本次原子移入归档表的 done 任务数；幂等事件和审计记录保留。
		Awaitable<std::uint64_t> archiveDone(std::string gameId,
											 unsigned keepDays,
											 unsigned batchSize,
											 std::string actor);
		/// @param gameId 游戏；keepDays 保留天数；batchSize 单批上限；actor 操作者。
		/// @return 已清空载荷的事件数；只处理全部任务均已归档的事件，业务键和摘要不变。
		Awaitable<std::uint64_t> redactCompleted(std::string gameId,
												 unsigned keepDays,
												 unsigned batchSize,
												 std::string actor);
		/// @param submission 通知任务；version 正版本号；content 规范化后的 JSON。@return 同一事务内发布配置和通知的结果。
		Awaitable<AcceptResult> publishConfig(Submission submission, std::uint64_t version, std::string content);
		/// @param gameId 游戏标识；version 为 0 时读取最新版本。@return 指定版本的配置；不存在时抛 404。
		Awaitable<boost::json::object> readConfig(std::string gameId, std::uint64_t version);
		/// @param gameId/serverId 已授权的游戏与区服；version 已应用版本；contentHash 内容摘要。@return 持久应用状态；旧版本或摘要不符抛 409。
		Awaitable<boost::json::object> reportConfigApplied(std::string gameId,
														   std::uint32_t serverId,
														   std::uint64_t version,
														   std::string contentHash);
		/// @param gameId 游戏；afterServer 区服游标；limit 为 1 到 100。@return 逐服已应用版本和摘要，不包含配置正文。
		Awaitable<boost::json::array> listConfigStates(std::string gameId,
													   std::uint32_t afterServer = 0,
													   unsigned limit = 100);
		/// @param gameId 游戏。@return 持久控制覆盖；没有记录时沿用启动配置。
		Awaitable<std::optional<DeliveryControl>> readDeliveryControl(std::string gameId);
		/// @param gameIds 本进程关心的至多 100 款游戏；空数组不查询。@return 已存在的控制覆盖。
		Awaitable<std::vector<DeliveryControl>> readDeliveryControls(std::vector<std::string> gameIds);
		/// @param desired 新值；expectedVersion 期望版本，0 表示首次创建；actor 操作人。@return 自动递增版本后的控制；冲突抛 409，更新与审计同事务。
		Awaitable<DeliveryControl> updateDeliveryControl(DeliveryControl desired,
														 std::uint64_t expectedVersion,
														 std::string actor);
		/// @param gameId 游戏标识；after ID 游标。@return 最多 100 条审计记录，不返回业务内容。
		Awaitable<boost::json::array> listAudit(std::string gameId, std::uint64_t after);

	private:
		// 每次调用拥有独立会话；仅外层入口按完整事务重试 1213，参数不会跨尝试被移空。
		Awaitable<AcceptResult> acceptOnce(Submission submission);
		Awaitable<std::optional<DeliveryTask>> claimOnce(const WorkerConfig& config,
														 std::optional<std::string> gameId,
														 std::vector<std::uint32_t> excludedServers);
		Awaitable<std::uint64_t> recoverOnce(const WorkerConfig& config, std::vector<std::string> gameIds);
		Awaitable<bool> finishOnce(const DeliveryTask& task,
								   bool success,
								   std::string error,
								   unsigned delay,
								   unsigned maxAttempts);
		Awaitable<bool> replayOnce(std::string gameId, std::uint64_t taskId, std::string actor);
		Awaitable<std::uint64_t> archiveDoneOnce(std::string gameId,
												 unsigned keepDays,
												 unsigned batchSize,
												 std::string actor);
		Awaitable<std::uint64_t> redactCompletedOnce(std::string gameId,
													 unsigned keepDays,
													 unsigned batchSize,
													 std::string actor);
		Awaitable<AcceptResult> publishConfigOnce(Submission submission, std::uint64_t version, std::string content);
		Awaitable<AcceptResult> insertEvent(DbSession& session, const Submission& submission);
		Awaitable<void> insertTasks(DbSession& session, const Submission& submission, std::uint64_t eventId);
		Awaitable<void> audit(DbSession& session,
							  std::string actor,
							  std::string action,
							  std::string gameId,
							  std::uint64_t targetId);
		std::shared_ptr<Database> database_;
	};
} // namespace gamehub
