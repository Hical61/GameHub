/** @file storage_tests.cpp
 * @brief 用集成脚本创建的临时库测试租约隔离和异常回滚。
 */
#include "core/Crypto.h"
#include "core/DeliveryProtocol.h"
#include "storage/Store.h"
#include <boost/asio/bind_cancellation_slot.hpp>
#include <boost/asio/cancel_after.hpp>
#include <boost/asio/cancellation_signal.hpp>
#include <boost/asio/co_spawn.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/this_coro.hpp>
#include <boost/asio/use_future.hpp>
#include <boost/mysql/common_server_errc.hpp>
#include <boost/mysql/error_categories.hpp>
#include <boost/mysql/mysql_server_errc.hpp>
#include <boost/system/system_error.hpp>
#include <iostream>

namespace
{
	void require(bool condition, const char* message)
	{
		if (!condition)
		{
			throw std::runtime_error(message);
		}
	}

	gamehub::Awaitable<gamehub::DeliveryTask> requireClaim(gamehub::Store& store,
														   const gamehub::WorkerConfig& worker,
														   const char* message)
	{
		auto task = co_await store.claim(worker);
		require(task.has_value(), message);
		co_return *task;
	}

	void requireTaskState(const boost::json::array& tasks,
						  const gamehub::DeliveryTask& task,
						  gamehub::TaskStatus status,
						  unsigned attempts,
						  std::uint64_t generation,
						  const char* message)
	{
		for (const auto& value : tasks)
		{
			const auto& item = value.as_object();
			if (item.at("id").as_string() == std::to_string(task.id))
			{
				require(item.at("status").to_number<unsigned>() == static_cast<unsigned>(status)
							&& item.at("attempts").to_number<unsigned>() == attempts
							&& item.at("generation").as_string() == std::to_string(generation),
						message);
				return;
			}
		}
		throw std::runtime_error(message);
	}

	gamehub::Awaitable<void> verifyLoweredRetryLimit(gamehub::Store& store, gamehub::WorkerConfig worker)
	{
		using namespace gamehub;
		constexpr unsigned kOriginalLimit = 4;
		constexpr unsigned kLoweredLimit = 2;
		worker.maxAttempts = kOriginalLimit;
		Submission submission {"demo", "announce", "admin", "retry-limit-active", "{}", "", {1}, "storage-test"};
		submission.fingerprint = sha256(submission.payload);
		co_await store.accept(submission);
		auto first = co_await requireClaim(store, worker, "未领取原上限的首次尝试");
		require(co_await store.finish(first, false, "network_error", 0, worker.maxAttempts), "原上限首次失败未记录");
		auto active = co_await requireClaim(store, worker, "未领取保持有效租约的任务");

		submission.bizKey = "retry-limit-eligible";
		co_await store.accept(submission);
		auto eligible = co_await requireClaim(store, worker, "未领取仍有剩余次数的任务");
		submission.bizKey = "retry-limit-exhausted";
		co_await store.accept(submission);
		auto previous = co_await requireClaim(store, worker, "未领取待耗尽任务的首次尝试");
		require(co_await store.finish(previous, false, "network_error", 0, worker.maxAttempts), "首次失败未进入重试");
		auto exhausted = co_await requireClaim(store, worker, "未领取待耗尽任务的第二次尝试");
		require(co_await store.finish(exhausted, false, "network_error", 0, worker.maxAttempts),
				"原上限未保留重试机会");
		require(co_await store.finish(eligible, false, "network_error", 0, worker.maxAttempts),
				"剩余次数任务未进入重试");

		worker.maxAttempts = kLoweredLimit;
		require((co_await store.recover(worker)) == 0, "下调上限回收了有效租约");
		require(!(co_await store.claim(worker)), "下调上限后仍投递耗尽任务");
		const auto tasks = co_await store.listTasks("demo", 0);
		requireTaskState(tasks,
						 exhausted,
						 TaskStatus::Failed,
						 kLoweredLimit,
						 exhausted.generation + 1,
						 "耗尽的 pending 未转为 failed 或未撤销旧代次");
		requireTaskState(tasks,
						 eligible,
						 TaskStatus::Pending,
						 eligible.attempts,
						 eligible.generation,
						 "单次领取修改了第二条 pending 任务");
		requireTaskState(tasks,
						 active,
						 TaskStatus::Delivering,
						 active.attempts,
						 active.generation,
						 "下调上限影响了有效投递租约");
		auto next = co_await requireClaim(store, worker, "下调上限后未领取仍有剩余次数的任务");
		require(next.id == eligible.id && next.attempts == kLoweredLimit, "剩余次数任务领取次数不正确");
		require(co_await store.finish(next, true, "", 0, worker.maxAttempts), "剩余次数任务无法确认");
		require(co_await store.finish(active, true, "", 0, worker.maxAttempts), "下调上限后有效租约无法确认");

		require(co_await store.replay("demo", exhausted.id, "storage-test"), "耗尽任务无法人工重排");
		auto replay = co_await requireClaim(store, worker, "人工重排后无法领取耗尽任务");
		require(replay.id == exhausted.id && replay.attempts == 1 && replay.generation > exhausted.generation,
				"耗尽任务重排未重置次数或代次");
		require(!(co_await store.finish(exhausted, true, "", 0, worker.maxAttempts)), "耗尽任务重排后旧代次仍能确认");
		require(co_await store.finish(replay, true, "", 0, worker.maxAttempts), "耗尽任务重排后无法完成");
	}

	gamehub::Awaitable<void> verifyRecoverySkipsActiveLocks(std::shared_ptr<gamehub::Database> database,
															gamehub::Store& store,
															gamehub::WorkerConfig worker)
	{
		using namespace gamehub;
		constexpr unsigned kRecoveryAttempts = 2;
		worker.maxAttempts = kRecoveryAttempts;
		Submission submission {"demo", "announce", "admin", "storage-recovery-lock", "{}", "", {1}, "storage-test"};
		submission.fingerprint = sha256(submission.payload);
		co_await store.accept(submission);
		auto active = co_await requireClaim(store, worker, "未领取回收锁测试任务");
		require(active.bizKey == submission.bizKey, "回收锁测试领取了其他任务");
		{
			auto session = co_await database->acquire();
			co_await session.query("START TRANSACTION");
			std::vector<std::string> key {std::to_string(active.id)};
			co_await session.query("SELECT id FROM delivery_task WHERE id=? FOR UPDATE", std::move(key));
			// 保持有效租约的主键锁，模拟 finish 已拿到主键、尚未更新次键的阶段。
			// 回收必须在释放这个锁之前返回；原范围 UPDATE 会在这里等锁直到查询超时。
			require((co_await store.recover(worker)) == 0, "回收修改了仍有效的租约");
			co_await session.query("COMMIT");
		}
		submission.bizKey = "storage-recovery-expired";
		co_await store.accept(submission);
		auto expired = co_await requireClaim(store, worker, "未领取待回收测试任务");
		require(expired.bizKey == submission.bizKey, "待回收测试领取了其他任务");
		{
			auto session = co_await database->acquire();
			std::vector<std::string> key {std::to_string(expired.id)};
			co_await session.query(
				"UPDATE delivery_task SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=?",
				std::move(key));
		}
		{
			auto session = co_await database->acquire();
			co_await session.query("START TRANSACTION");
			std::vector<std::string> key {std::to_string(active.id)};
			co_await session.query("SELECT id FROM delivery_task WHERE id=? FOR UPDATE", std::move(key));
			// 非空批次也不能扫描未选中的有效租约；二级索引范围更新会再次等这个主键锁。
			require((co_await store.recover(worker)) == 1, "回收批次未只处理选中的过期任务");
			co_await session.query("COMMIT");
		}
		requireTaskState(co_await store.listTasks("demo", 0),
						 expired,
						 TaskStatus::Pending,
						 expired.attempts,
						 expired.generation + 1,
						 "过期任务未回到 pending 或未撤销旧代次");
		auto retry = co_await requireClaim(store, worker, "回收后未重新领取过期任务");
		require(retry.id == expired.id, "回收后领取了其他任务");
		require(co_await store.finish(retry, true, "", 0, worker.maxAttempts), "回收后的任务无法确认");
		require(co_await store.finish(active, true, "", 0, worker.maxAttempts), "回收后有效租约无法确认");
	}

	gamehub::Awaitable<void> verifyArchiveSkipsUnselectedLocks(std::shared_ptr<gamehub::Database> database,
															   gamehub::Store& store,
															   const gamehub::WorkerConfig& worker)
	{
		using namespace gamehub;
		Submission submission {"demo", "announce", "admin", "storage-archive-active", "{}", "", {1}, "storage-test"};
		submission.fingerprint = sha256(submission.payload);
		co_await store.accept(submission);
		auto active = co_await requireClaim(store, worker, "未领取归档锁测试的有效任务");
		require(active.bizKey == submission.bizKey, "归档锁测试领取了其他任务");
		submission.bizKey = "storage-archive-selected";
		co_await store.accept(submission);
		auto selected = co_await requireClaim(store, worker, "未领取待归档测试任务");
		require(selected.bizKey == submission.bizKey, "待归档测试领取了其他任务");
		require(co_await store.finish(selected, true, "", 0, worker.maxAttempts), "待归档任务无法完成");
		{
			auto session = co_await database->acquire();
			std::vector<std::string> key {std::to_string(selected.id)};
			co_await session.query(
				"UPDATE delivery_task SET updated_at=TIMESTAMPADD(DAY,-3,UTC_TIMESTAMP(6)) WHERE id=?",
				std::move(key));
		}
		{
			auto session = co_await database->acquire();
			co_await session.query("START TRANSACTION");
			std::vector<std::string> key {std::to_string(active.id)};
			co_await session.query("SELECT id FROM delivery_task WHERE id=? FOR UPDATE", std::move(key));
			// 单批归档不能扫描并锁住这条未选中的有效任务。
			require((co_await store.archiveDone("demo", 1, 1, "storage-test")) == 1, "归档未处理选中的一条任务");
			co_await session.query("COMMIT");
		}
		{
			auto session = co_await database->acquire();
			std::vector<std::string> keys {std::to_string(selected.id), std::to_string(selected.id)};
			auto result =
				co_await session.query("SELECT IF(EXISTS(SELECT 1 FROM delivery_task_archive WHERE id=?) "
									   "AND NOT EXISTS(SELECT 1 FROM delivery_task WHERE id=?),'complete','invalid')",
									   std::move(keys));
			require(result.rows()[0][0].as_string() == "complete", "归档任务未完整迁移或仍留在活动表");
			std::vector<std::string> key {std::to_string(selected.id)};
			co_await session.query("DELETE FROM delivery_task_archive WHERE id=?", std::move(key));
		}
		require(co_await store.finish(active, true, "", 0, worker.maxAttempts), "归档后未选中任务的有效租约无法确认");
	}

	gamehub::Awaitable<void> verifyNonDeadlockIsNotRetried(std::shared_ptr<gamehub::Database> database,
														   gamehub::Store& store,
														   const gamehub::WorkerConfig& worker)
	{
		using namespace gamehub;
		Submission submission {"demo", "announce", "admin", "storage-no-retry-before", "{}", "", {1}, "storage-test"};
		submission.fingerprint = sha256(submission.payload);
		const auto before = co_await store.accept(submission);
		auto first = co_await requireClaim(store, worker, "未领取非死锁重试测试的起始任务");
		require(co_await store.finish(first, true, "", 0, worker.maxAttempts), "起始任务无法确认");

		submission.bizKey = "storage-no-retry-failed";
		submission.servers = {1, 1};
		bool duplicateTarget = false;
		try
		{
			co_await store.accept(submission);
		}
		catch (const boost::system::system_error& error)
		{
			duplicateTarget = error.code() == boost::mysql::common_server_errc::er_dup_entry;
		}
		require(duplicateTarget, "重复区服未返回原始非死锁错误");
		submission.bizKey = "storage-no-retry-after";
		submission.servers = {1};
		const auto after = co_await store.accept(submission);
		// 单行事件 INSERT 每尝试消耗一个自增 ID；失败后只允许留下一个空号。
		require(after.eventId == before.eventId + 2, "非 1213 错误被重试了多次");
		{
			auto session = co_await database->acquire();
			auto remaining = co_await session.query("SELECT id FROM hub_event WHERE biz_key='storage-no-retry-failed'");
			require(remaining.rows().empty(), "非死锁失败泄漏了未提交事件");
		}
		auto last = co_await requireClaim(store, worker, "错误后连接池未恢复");
		require(co_await store.finish(last, true, "", 0, worker.maxAttempts), "错误后任务无法确认");
	}

	gamehub::Awaitable<void> verifyScopedClaims(gamehub::Store& store, gamehub::WorkerConfig worker)
	{
		using namespace gamehub;
		worker.maxAttempts = 2;
		Submission input {"storage-beta",
						  "announce",
						  "storage",
						  "scoped-claim",
						  "{\"synthetic\":true}",
						  "",
						  {1},
						  "storage-test"};
		input.fingerprint = sha256(input.payload);
		co_await store.accept(input);
		input.gameId = "storage-alpha";
		input.servers = {1, 2};
		input.payloadVersion = 2;
		const auto accepted = co_await store.accept(input);
		auto task = co_await store.claim(worker, input.gameId, {1});
		require(task && task->gameId == input.gameId && task->serverId == 2, "范围领取跨游戏或未排除满额区服");
		require(task->eventId == accepted.eventId && task->nameSpace == input.nameSpace
					&& canonicalJson(parseObject(task->payload)) == input.payload && task->payloadVersion == 2
					&& task->fingerprintVersion == kCurrentFingerprintVersion,
				"领取任务丢失原始载荷、身份或持久化版本");
		const auto envelope = parseObject(encodeDelivery(*task));
		require(envelope.at("schemaVersion").to_number<unsigned>() == kDeliverySchemaVersion
					&& envelope.at("payloadVersion").to_number<unsigned>() == 2
					&& envelope.at("taskId").as_string() == std::to_string(task->id)
					&& envelope.at("eventId").as_string() == std::to_string(accepted.eventId)
					&& envelope.at("namespace").as_string() == input.nameSpace
					&& canonicalJson(envelope.at("payload")) == input.payload,
				"协议编码未保留既有字段与业务载荷");
		require(!(co_await store.claim(worker, input.gameId, {1, 2})), "全部目标满额后仍领取任务");
		require(!(co_await store.claim(worker, "storage-empty")), "空游戏领取了其他游戏任务");
		bool rejectedEmpty = false;
		try
		{
			co_await store.claim(worker, "");
		}
		catch (const std::invalid_argument&)
		{
			rejectedEmpty = true;
		}
		require(rejectedEmpty, "空游戏范围退化为全局领取");
		const auto alpha = co_await store.stats(input.gameId);
		const auto beta = co_await store.stats("storage-beta");
		require(alpha.at("pending").to_number<unsigned>() == 1 && alpha.at("delivering").to_number<unsigned>() == 1
					&& beta.at("pending").to_number<unsigned>() == 1
					&& beta.at("delivering").to_number<unsigned>() == 0,
				"按游戏统计串入其他游戏或排除目标消耗了尝试次数");
		require(co_await store.finish(*task, true, "", 0, worker.maxAttempts), "范围领取任务无法完成");
		auto remaining = co_await store.claim(worker, input.gameId);
		require(remaining && remaining->serverId == 1 && remaining->attempts == 1, "排除区服被隐式领取过");
		require(co_await store.finish(*remaining, true, "", 0, worker.maxAttempts), "剩余目标无法完成");
		auto other = co_await store.claim(worker, "storage-beta");
		require(other && other->gameId == "storage-beta", "其他游戏任务丢失");
		require(co_await store.finish(*other, true, "", 0, worker.maxAttempts), "其他游戏任务无法完成");
		input.payloadVersion = 1;
		bool versionConflict = false;
		try
		{
			co_await store.accept(input);
		}
		catch (const ApiError& error)
		{
			versionConflict = error.status == 409;
		}
		require(versionConflict, "重试可改变同一业务身份的持久化版本");
	}

	gamehub::Awaitable<void> verifyScopedRecovery(std::shared_ptr<gamehub::Database> database,
												  gamehub::Store& store,
												  gamehub::WorkerConfig worker)
	{
		using namespace gamehub;
		worker.maxAttempts = 2;
		Submission input {"storage-alpha", "announce", "storage", "scoped-recovery", "{}", "", {1}, "storage-test"};
		input.fingerprint = sha256(input.payload);
		co_await store.accept(input);
		auto alpha = co_await store.claim(worker, input.gameId);
		input.gameId = "storage-beta";
		co_await store.accept(input);
		auto beta = co_await store.claim(worker, input.gameId);
		require(alpha && beta, "范围回收测试未能领取两款游戏");
		{
			auto session = co_await database->acquire();
			std::vector<std::string> keys {std::to_string(alpha->id), std::to_string(beta->id)};
			co_await session.query(
				"UPDATE delivery_task SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id IN (?,?)",
				keys);
		}
		require((co_await store.recover(worker, {})) == 0, "空回收范围修改了任务");
		require((co_await store.recover(worker, {"storage-alpha"})) == 1, "范围回收未仅处理选中游戏");
		requireTaskState(co_await store.listTasks("storage-beta", 0),
						 *beta,
						 TaskStatus::Delivering,
						 beta->attempts,
						 beta->generation,
						 "范围回收修改了其他游戏的租约");
		require(!(co_await store.finish(*beta, true, "", 0, worker.maxAttempts)), "未回收的过期租约仍可确认");
		require((co_await store.recover(worker, {"storage-beta"})) == 1, "第二款游戏无法独立回收");
		for (const auto* gameId : {"storage-alpha", "storage-beta"})
		{
			auto replacement = co_await store.claim(worker, gameId);
			require(replacement && replacement->attempts == 2, "范围回收后尝试次数不正确");
			require(co_await store.finish(*replacement, true, "", 0, worker.maxAttempts), "范围回收后无法确认");
		}
	}

	struct RecoveryCompletion
	{
		bool done = false;
		std::uint64_t count = 0;
		std::exception_ptr error;
	};

	gamehub::Awaitable<void> awaitRecoveryCompletion(std::shared_ptr<RecoveryCompletion> completion)
	{
		boost::asio::steady_timer pause(co_await boost::asio::this_coro::executor);
		while (!completion->done)
		{
			pause.expires_after(std::chrono::milliseconds(5));
			co_await pause.async_wait(boost::asio::use_awaitable);
		}
	}

	gamehub::Awaitable<bool> taskAvailableNowait(gamehub::DbSession& session, std::string taskId)
	{
		co_await session.query("START TRANSACTION");
		bool available = true;
		std::exception_ptr failure;
		try
		{
			std::vector<std::string> key {std::move(taskId)};
			co_await session.query("SELECT id FROM delivery_task WHERE id=? FOR UPDATE NOWAIT", std::move(key));
		}
		catch (const boost::system::system_error& error)
		{
			const boost::system::error_code locked {boost::mysql::mysql_server_errc::er_lock_nowait,
													boost::mysql::get_mysql_server_category()};
			if (error.code() == locked)
			{
				available = false;
			}
			else
			{
				failure = std::current_exception();
			}
		}
		co_await session.query("ROLLBACK");
		if (failure)
		{
			std::rethrow_exception(failure);
		}
		co_return available;
	}

	gamehub::Awaitable<void> verifyRecoveryLockWindow(gamehub::DbSession& observer,
													  const boost::mysql::results& bounds,
													  std::shared_ptr<RecoveryCompletion> completion)
	{
		auto executor = co_await boost::asio::this_coro::executor;
		const std::string firstId(bounds.rows()[0][0].as_string());
		const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
		for (;;)
		{
			// 计数触发器使用迁移账号身份；临时测试实例在本子进程期间授权 PROCESS，只返回布尔值。
			// UPDATE 已开始且被计数门闩阻塞，说明领取 SELECT 已结束，此后探测不会干扰 SKIP LOCKED。
			auto updating = co_await observer.query(
				"SELECT IF(EXISTS(SELECT 1 FROM information_schema.PROCESSLIST WHERE DB=DATABASE() "
				"AND ID<>CONNECTION_ID() AND INFO LIKE 'UPDATE delivery_task_counts SET %'),'yes','no')");
			if (updating.rows()[0][0].as_string() == "yes")
			{
				break;
			}
			require(!completion->done && std::chrono::steady_clock::now() < deadline, "回收未在计数门闩前开始更新任务");
			boost::asio::steady_timer pause(executor, std::chrono::milliseconds(5));
			co_await pause.async_wait(boost::asio::use_awaitable);
		}
		require(!(co_await taskAvailableNowait(observer, firstId)), "回收更新时未持有选中任务的锁");
		for (auto row : bounds.rows())
		{
			require(co_await taskAvailableNowait(observer, std::string(row[1].as_string())),
					"回收在提交前锁住了批次外任务");
		}
	}

	gamehub::Awaitable<std::uint64_t> recoverWithLockProbe(std::shared_ptr<gamehub::Database> database,
														   gamehub::Store& store,
														   const gamehub::WorkerConfig& worker,
														   bool cancel)
	{
		using namespace gamehub;
		auto executor = co_await boost::asio::this_coro::executor;
		auto completion = std::make_shared<RecoveryCompletion>();
		boost::asio::cancellation_signal cancellation;
		std::exception_ptr failure;
		{
			auto gate = co_await database->acquire();
			auto bounds = co_await gate.query(
				"SELECT CAST(MIN(id) AS CHAR),CAST(MAX(id) AS CHAR) FROM delivery_task "
				"WHERE game_id IN ('storage-batch-alpha','storage-batch-beta') GROUP BY game_id ORDER BY game_id");
			const std::string firstId(bounds.rows()[0][0].as_string());
			co_await gate.query("START TRANSACTION");
			std::vector<std::string> key {firstId};
			co_await gate.query("SELECT bucket_id FROM delivery_task_counts WHERE bucket_id=MOD(?,64) FOR UPDATE",
								std::move(key));
			const auto completed = [completion](std::exception_ptr error, std::uint64_t count)
			{
				completion->error = error;
				completion->count = count;
				completion->done = true;
			};
			std::vector<std::string> games {"storage-batch-alpha", "storage-batch-beta"};
			boost::asio::co_spawn(executor,
								  store.recover(worker, games),
								  boost::asio::bind_cancellation_slot(cancellation.slot(), completed));
			try
			{
				auto observer = co_await database->acquire();
				co_await verifyRecoveryLockWindow(observer, bounds, completion);
				if (cancel)
				{
					cancellation.emit(boost::asio::cancellation_type::terminal);
					co_await awaitRecoveryCompletion(completion);
				}
				co_await gate.query("ROLLBACK");
			}
			catch (...)
			{
				failure = std::current_exception();
			}
		}
		// 异常也先释放计数门闩，再等子协程退出，worker 和 Store 的父帧始终存活。
		co_await boost::asio::this_coro::reset_cancellation_state(boost::asio::disable_cancellation());
		co_await awaitRecoveryCompletion(completion);
		co_await boost::asio::this_coro::reset_cancellation_state();
		if (failure)
		{
			std::rethrow_exception(failure);
		}
		if (completion->error)
		{
			std::rethrow_exception(completion->error);
		}
		co_return completion->count;
	}

	gamehub::Awaitable<void> verifyRecoveryBatchState(std::shared_ptr<gamehub::Database> database,
													  gamehub::Store& store,
													  unsigned pending)
	{
		using namespace gamehub;
		constexpr unsigned kTasksPerGame = 120;
		for (const auto* gameId : {"storage-batch-alpha", "storage-batch-beta"})
		{
			const auto counts = co_await store.stats(gameId);
			require(counts.at("pending").to_number<unsigned>() == pending
						&& counts.at("delivering").to_number<unsigned>() == kTasksPerGame - pending,
					"分游戏回收未平均分配批次或修改了批次外任务");
		}
		const auto outside = co_await store.stats("storage-batch-outside");
		require(outside.at("delivering").to_number<unsigned>() == 1 && outside.at("pending").to_number<unsigned>() == 0,
				"分游戏回收修改了负责范围外任务");
		auto session = co_await database->acquire();
		auto generation =
			co_await session.query("SELECT IF(COUNT(*)=0,'valid','invalid') FROM delivery_task WHERE game_id IN "
								   "('storage-batch-alpha','storage-batch-beta','storage-batch-outside') "
								   "AND ((status=0 AND generation<>8) OR (status=1 AND generation<>7) OR attempts<>1)");
		require(generation.rows()[0][0].as_string() == "valid", "回收重复递增了代次或修改了批次外代次、尝试次数");
	}

	gamehub::Awaitable<void> verifyScopedRecoveryBatches(std::shared_ptr<gamehub::Database> database,
														 gamehub::Store& store,
														 gamehub::WorkerConfig worker)
	{
		using namespace gamehub;
		constexpr unsigned kTasksPerGame = 120;
		worker.maxAttempts = 2;
		Submission input {"storage-batch-alpha", "announce", "storage", "scoped-batch", "{}", "", {}, "storage-test"};
		input.fingerprint = sha256(input.payload);
		for (unsigned serverId = 1; serverId <= kTasksPerGame; ++serverId)
		{
			input.servers.push_back(serverId);
		}
		co_await store.accept(input);
		input.gameId = "storage-batch-beta";
		co_await store.accept(input);
		input.gameId = "storage-batch-outside";
		input.servers = {1};
		co_await store.accept(input);
		{
			auto session = co_await database->acquire();
			// 合成进程崩溃后的持久化租约，不逐条领取，避免放大集成测试的数据库往返。
			co_await session.query(
				"UPDATE delivery_task SET status=1,attempts=1,generation=7,"
				"lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) "
				"WHERE game_id IN ('storage-batch-alpha','storage-batch-beta','storage-batch-outside')");
		}
		const std::vector<std::string> games {"storage-batch-alpha", "storage-batch-beta"};
		const auto countsBeforeCancel = co_await store.stats();
		bool cancelled = false;
		try
		{
			co_await recoverWithLockProbe(database, store, worker, true);
		}
		catch (const boost::system::system_error& error)
		{
			cancelled = error.code() == boost::asio::error::operation_aborted;
		}
		require(cancelled, "计数更新前取消回收未返回取消错误");
		co_await verifyRecoveryBatchState(database, store, 0);
		require((co_await store.stats()) == countsBeforeCancel, "取消回收未恢复全局事务计数");
		require((co_await recoverWithLockProbe(database, store, worker, false)) == 100, "多游戏回收首批未满足总额上限");
		co_await verifyRecoveryBatchState(database, store, 50);
		require((co_await store.recover(worker, games)) == 100, "多游戏回收第二批未满足总额上限");
		co_await verifyRecoveryBatchState(database, store, 100);
		require((co_await store.recover(worker, games)) == 40, "多游戏回收未处理剩余任务");
		co_await verifyRecoveryBatchState(database, store, kTasksPerGame);
		require((co_await store.recover(worker, games)) == 0, "回收修改了已经回收的任务");
		{
			auto session = co_await database->acquire();
			co_await session.query("DELETE FROM delivery_task WHERE game_id IN "
								   "('storage-batch-alpha','storage-batch-beta','storage-batch-outside')");
		}
	}

	gamehub::Awaitable<void> verifyGameCounts(std::shared_ptr<gamehub::Database> database, gamehub::Store& store)
	{
		using namespace gamehub;
		const auto before = co_await store.stats("storage-alpha");
		Submission
			input {"storage-alpha", "announce", "storage", "game-count-rollback", "{}", "", {1, 1}, "storage-test"};
		input.fingerprint = sha256(input.payload);
		bool failed = false;
		try
		{
			co_await store.accept(input);
		}
		catch (const boost::system::system_error& error)
		{
			failed = error.code() == boost::mysql::common_server_errc::er_dup_entry;
		}
		require(failed && (co_await store.stats("storage-alpha")) == before, "受理回滚未恢复按游戏事务计数");
		{
			auto session = co_await database->acquire();
			co_await session.query(
				"UPDATE delivery_task SET updated_at=TIMESTAMPADD(DAY,-3,UTC_TIMESTAMP(6)) WHERE game_id='storage-alpha'");
		}
		const auto beta = co_await store.stats("storage-beta");
		const auto archived = co_await store.archiveDone("storage-alpha", 1, kMaxMaintenanceBatch, "storage-test");
		const auto after = co_await store.stats("storage-alpha");
		require(archived == before.at("done").to_number<std::uint64_t>() && after.at("done").to_number<unsigned>() == 0
					&& after.at("archivedDone").to_number<std::uint64_t>() == archived
					&& after.at("completedTotal") == before.at("completedTotal"),
				"归档改变了按游戏完成总数");
		require((co_await store.stats("storage-beta")) == beta, "归档污染其他游戏计数");
		const auto empty = co_await store.stats("storage-empty");
		require(empty.at("pending").to_number<unsigned>() == 0 && empty.at("completedTotal").to_number<unsigned>() == 0,
				"无任务游戏统计未返回零值");
		const auto queries = database->metrics().queries;
		std::vector<std::string> games {"storage-alpha", "storage-beta", "storage-empty"};
		const auto grouped = co_await store.gameStats(games);
		require(database->metrics().queries == queries + 1 && grouped.size() == 3, "批量游戏统计没有使用一次有界 SQL");
		for (const auto& item : grouped)
		{
			const auto& value = item.as_object();
			const auto& expected =
				value.at("gameId") == "storage-alpha" ? after : (value.at("gameId") == "storage-beta" ? beta : empty);
			for (const auto* key : {"pending", "delivering", "done", "failed", "archivedDone", "oldestPendingSeconds"})
			{
				require(value.at(key) == expected.at(key), "批量游戏统计与单游戏统计不一致");
			}
		}
		{
			auto session = co_await database->acquire();
			co_await session.query("DELETE FROM delivery_task WHERE game_id IN ('storage-alpha','storage-beta')");
			co_await session.query(
				"DELETE FROM delivery_task_archive WHERE game_id IN ('storage-alpha','storage-beta')");
		}
		require((co_await store.stats("storage-alpha")).at("completedTotal").to_number<unsigned>() == 0
					&& (co_await store.stats("storage-beta")).at("completedTotal").to_number<unsigned>() == 0,
				"删除任务或归档后按游戏计数未归零");
	}

	gamehub::Awaitable<void> verifyCancelledSession(gamehub::Config config)
	{
		using namespace gamehub;
		constexpr unsigned kSingleConnection = 1;
		constexpr auto kCancelDelay = std::chrono::milliseconds(100);
		config.dbMaxConnections = kSingleConnection;
		auto executor = co_await boost::asio::this_coro::executor;
		auto database = std::make_shared<Database>(executor, config);
		boost::asio::co_spawn(executor,
							  database->run(),
							  [database](std::exception_ptr)
							  {
							  });
		std::exception_ptr failure;
		try
		{
			std::string previousId;
			{
				auto session = co_await database->acquire();
				auto identity = co_await session.query("SELECT CAST(CONNECTION_ID() AS CHAR)");
				previousId = std::string(identity.rows()[0][0].as_string());
				co_await session.query("START TRANSACTION");
				co_await session.query(
					"INSERT INTO hub_event(game_id,task_type,namespace_key,biz_key,request_hash,payload,created_at) "
					"VALUES('demo','announce','storage','storage-cancelled-session',REPEAT('0',64),'{}',UTC_TIMESTAMP(6))");
				bool cancelled = false;
				try
				{
					// 只取消这个子协程，DbSession 自身的五秒查询上限保持不变。
					co_await boost::asio::co_spawn(executor,
												   session.query("DO SLEEP(0.4)"),
												   boost::asio::cancel_after(kCancelDelay, boost::asio::use_awaitable));
				}
				catch (const boost::system::system_error& error)
				{
					cancelled = error.code() == boost::asio::error::operation_aborted;
				}
				require(cancelled, "慢查询未按子协程取消信号结束");
			}
			{
				auto session = co_await database->acquire();
				std::vector<std::string> key {"storage-cancelled-session"};
				auto result =
					co_await session.query("SELECT CAST(CONNECTION_ID() AS CHAR),"
										   "IF(EXISTS(SELECT 1 FROM hub_event WHERE biz_key=?),'leaked','rolled_back')",
										   std::move(key));
				require(result.rows().size() == 1 && std::string(result.rows()[0][0].as_string()) != previousId,
						"取消后的单连接池复用了旧连接或读到了残留响应");
				require(result.rows()[0][1].as_string() == "rolled_back", "取消后事务未回滚或被自动重放");
			}
		}
		catch (...)
		{
			failure = std::current_exception();
		}
		database->stop();
		if (failure)
		{
			std::rethrow_exception(failure);
		}
	}

	gamehub::Awaitable<void> verifyAttemptHistory(std::shared_ptr<gamehub::Database> database,
												  gamehub::Store& store,
												  gamehub::WorkerConfig worker)
	{
		using namespace gamehub;
		worker.maxAttempts = 1;
		Submission input {"storage-history",
						  "announce",
						  "history",
						  "attempts",
						  "{\"privateSynthetic\":true}",
						  "",
						  {1},
						  "storage-test"};
		input.fingerprint = sha256(input.payload);
		co_await store.accept(input);
		auto task = co_await store.claim(worker, input.gameId);
		require(task.has_value(), "没有领取历史测试任务");
		auto attempts = co_await store.listAttempts(input.gameId, task->id);
		require(attempts.size() == 1 && attempts[0].as_object().at("finishedAt").is_null(), "领取没有原子创建在途尝试");
		require(co_await store.finish(*task, false, "禁止保存的合成原始响应", 0, 1), "未记录首次失败");
		attempts = co_await store.listAttempts(input.gameId, task->id);
		require(attempts[0].as_object().at("outcome") == "failure"
					&& attempts[0].as_object().at("errorCategory") == "delivery_exception",
				"尝试记录保存了非固定错误");
		require(co_await store.replay(input.gameId, task->id, "storage-test"), "历史测试任务不能重排");
		auto next = co_await store.claim(worker, input.gameId);
		require(next && next->generation > task->generation && next->attempts == 1, "重排未保留代次且重置尝试次数");
		{
			auto session = co_await database->acquire();
			std::vector<std::string> key {std::to_string(next->id)};
			co_await session.query(
				"UPDATE delivery_task SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=?",
				key);
		}
		require(!(co_await store.finish(*next, true, "", 0, 1)), "过期租约错误完成尝试");
		require((co_await store.recover(worker, {input.gameId})) == 1, "过期历史测试任务未回收");
		attempts = co_await store.listAttempts(input.gameId, task->id);
		require(attempts.size() == 2 && attempts[1].as_object().at("outcome") == "lease_expired",
				"回收未结束对应代次尝试");
		require(co_await store.replay(input.gameId, task->id, "storage-test"), "回收任务不能重排");
		auto final = co_await store.claim(worker, input.gameId);
		require(final && co_await store.finish(*final, true, "", 0, 1), "历史测试任务最终确认失败");
		{
			auto session = co_await database->acquire();
			std::vector<std::string> key {std::to_string(task->id)};
			co_await session.query(
				"UPDATE delivery_task SET updated_at=TIMESTAMPADD(DAY,-3,UTC_TIMESTAMP(6)) WHERE id=?",
				key);
		}
		require((co_await store.archiveDone(input.gameId, 1, 10, "storage-test")) == 1, "历史任务不能归档");
		attempts = co_await store.listAttempts(input.gameId, task->id);
		require(attempts.size() == 3 && attempts[2].as_object().at("outcome") == "success", "归档丢失投递尝试历史");
		auto event = co_await store.lookupEvent(input.gameId, input.taskType, input.nameSpace, input.bizKey);
		require(event.at("tasks").as_array().size() == 1
					&& event.at("tasks").as_array()[0].as_object().at("archived").as_bool(),
				"业务身份不能查归档任务");
		require(boost::json::serialize(event).find("privateSynthetic") == std::string::npos, "事件检索泄露业务载荷");
	}

	gamehub::Awaitable<void> verifyAttemptRollback(std::shared_ptr<gamehub::Database> database,
												   gamehub::Store& store,
												   const gamehub::WorkerConfig& worker)
	{
		using namespace gamehub;
		Submission
			input {"storage-attempt-atomic", "announce", "history", "atomic", "{}", sha256("{}"), {1}, "storage-test"};
		co_await store.accept(input);
		{
			auto session = co_await database->acquire();
			co_await session.query("CREATE TRIGGER synthetic_attempt_insert BEFORE INSERT ON delivery_attempt "
								   "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic'");
		}
		bool rejected = false;
		try
		{
			co_await store.claim(worker, input.gameId);
		}
		catch (const boost::system::system_error&)
		{
			rejected = true;
		}
		{
			auto session = co_await database->acquire();
			co_await session.query("DROP TRIGGER synthetic_attempt_insert");
		}
		require(rejected, "尝试写入失败没有传播");
		auto task = co_await store.claim(worker, input.gameId);
		require(task && task->attempts == 1 && task->generation == 1, "尝试写入失败未回滚领取事务");
		{
			auto session = co_await database->acquire();
			co_await session.query("CREATE TRIGGER synthetic_attempt_update BEFORE UPDATE ON delivery_attempt "
								   "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic'");
		}
		rejected = false;
		try
		{
			co_await store.finish(*task, true, "", 0, worker.maxAttempts);
		}
		catch (const boost::system::system_error&)
		{
			rejected = true;
		}
		{
			auto session = co_await database->acquire();
			co_await session.query("DROP TRIGGER synthetic_attempt_update");
		}
		require(rejected && co_await store.finish(*task, true, "", 0, worker.maxAttempts),
				"尝试结束失败未回滚任务确认");
	}

	gamehub::Awaitable<void> verifyTaskFilters(gamehub::Store& store, gamehub::WorkerConfig worker)
	{
		using namespace gamehub;
		Submission
			input {"storage-filter", "announce", "history", "filters", "{}", sha256("{}"), {1, 2}, "storage-test"};
		co_await store.accept(input);
		TaskFilter filter;
		filter.status = TaskStatus::Pending;
		filter.serverId = 2;
		filter.createdFrom = unixSeconds() - 60;
		filter.createdBefore = unixSeconds() + 60;
		filter.limit = 1;
		auto tasks = co_await store.listTasks(input.gameId, filter);
		require(tasks.size() == 1 && tasks[0].as_object().at("serverId").to_number<unsigned>() == 2,
				"任务筛选范围错误");
		filter.after = std::stoull(std::string(tasks[0].as_object().at("id").as_string()));
		require((co_await store.listTasks(input.gameId, filter)).empty(), "筛选游标重复返回任务");
		filter.limit = 101;
		bool rejected = false;
		try
		{
			co_await store.listTasks(input.gameId, filter);
		}
		catch (const ApiError& error)
		{
			rejected = error.status == 400;
		}
		require(rejected, "任务分页未限制数量");
		for (unsigned count = 0; count < 2; ++count)
		{
			auto task = co_await store.claim(worker, input.gameId);
			require(task && co_await store.finish(*task, true, "", 0, worker.maxAttempts), "筛选测试任务未完成");
		}
	}

	gamehub::Awaitable<void> verifyConfigState(gamehub::Store& store)
	{
		using namespace gamehub;
		Submission
			input {"storage-config-state", "config", "admin", "config:1", "{}", sha256("{}"), {1}, "storage-test"};
		co_await store.publishConfig(input, 1, "{\"version\":1}");
		input.bizKey = "config:2";
		co_await store.publishConfig(input, 2, "{\"version\":2}");
		auto report = co_await store.reportConfigApplied(input.gameId, 1, 2, sha256("{\"version\":2}"));
		require(report.at("applied").as_bool(), "配置应用报告未受理");
		co_await store.reportConfigApplied(input.gameId, 1, 2, sha256("{\"version\":2}"));
		bool stale = false;
		try
		{
			co_await store.reportConfigApplied(input.gameId, 1, 1, sha256("{\"version\":1}"));
		}
		catch (const ApiError& error)
		{
			stale = error.status == 409;
		}
		require(stale, "旧应用报告覆盖新状态");
		bool mismatch = false;
		try
		{
			co_await store.reportConfigApplied(input.gameId, 2, 2, sha256("wrong"));
		}
		catch (const ApiError& error)
		{
			mismatch = error.status == 409;
		}
		auto states = co_await store.listConfigStates(input.gameId);
		require(mismatch && states.size() == 1 && states[0].as_object().at("version").to_number<unsigned>() == 2,
				"摘要不匹配仍写入应用状态");
		require((co_await store.listConfigStates(input.gameId, 1)).empty(), "配置状态分页重复");
		bool missing = false;
		try
		{
			co_await store.reportConfigApplied(input.gameId, 3, 3, sha256("{}"));
		}
		catch (const ApiError& error)
		{
			missing = error.status == 404;
		}
		require(missing, "不存在的配置版本仍能报告已应用");
		WorkerConfig worker;
		for (unsigned count = 0; count < 2; ++count)
		{
			auto task = co_await store.claim(worker, input.gameId);
			require(task && co_await store.finish(*task, true, "", 0, worker.maxAttempts), "配置状态测试任务未完成");
		}
	}

	gamehub::Awaitable<void> verifyConfigCanonicalBytes(gamehub::Store& store)
	{
		using namespace gamehub;
		const auto original = parseObject(R"({"z":1.0,"small":0.00001,"large":1e20,"text":"合成配置","zero":-0.0})");
		const auto canonical = canonicalJson(original);
		Submission input {"storage-config-numeric",
						  "config_push",
						  "admin",
						  "numeric:1",
						  "{}",
						  sha256("{}"),
						  {1},
						  "storage-test"};
		co_await store.publishConfig(input, 1, canonical);
		const auto restored = co_await store.readConfig(input.gameId, 1);
		const auto wire = std::string(restored.at("canonicalContent").as_string());
		require(wire == canonical && sha256(wire) == restored.at("sha256").as_string(),
				"MySQL 读回的浮点/字符串配置没有保持可跨语言校验的规范字节");
		co_await store.reportConfigApplied(input.gameId, 1, 1, sha256(wire));
		WorkerConfig worker;
		auto task = co_await store.claim(worker, input.gameId);
		require(task && co_await store.finish(*task, true, "", 0, worker.maxAttempts), "数字配置测试通知未完成");
	}

	gamehub::Awaitable<void> verifyDeliveryControl(gamehub::Store& store)
	{
		using namespace gamehub;
		const std::string game = "storage-control";
		require(!(co_await store.readDeliveryControl(game)), "没有持久控制时未返回空覆盖");
		DeliveryControl desired {game, 0, false, 3, 1};
		auto first = co_await store.updateDeliveryControl(desired, 0, "storage-test");
		require(first.version == 1 && !first.enabled, "首次控制未创建版本 1");
		bool conflict = false;
		try
		{
			co_await store.updateDeliveryControl(desired, 0, "storage-test");
		}
		catch (const ApiError& error)
		{
			conflict = error.status == 409;
		}
		require(conflict, "控制创建竞争未拒绝旧版本");
		desired.enabled = true;
		auto second = co_await store.updateDeliveryControl(desired, first.version, "storage-test");
		auto stored = co_await store.readDeliveryControl(game);
		require(stored && stored->version == 2 && second.version == 2 && stored->enabled, "控制更新未持久化新版本");
		conflict = false;
		try
		{
			co_await store.updateDeliveryControl(desired, first.version, "storage-test");
		}
		catch (const ApiError& error)
		{
			conflict = error.status == 409;
		}
		require(conflict, "控制旧版本 CAS 更新未拒绝");
		auto audits = co_await store.listAudit(game, 0);
		require(audits.size() == 2 && audits[0].as_object().at("action") == "delivery_control",
				"CAS 冲突产生审计或成功更新漏审计");
		std::vector<std::string> games {game, "storage-empty-control"};
		require((co_await store.readDeliveryControls(games)).size() == 1, "批量控制查询超出游戏范围");
	}

	gamehub::Awaitable<void> verifyControlAuditRollback(std::shared_ptr<gamehub::Database> database,
														gamehub::Store& store)
	{
		using namespace gamehub;
		{
			auto session = co_await database->acquire();
			co_await session.query("CREATE TRIGGER synthetic_control_audit BEFORE INSERT ON audit_log FOR EACH ROW "
								   "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic'");
		}
		DeliveryControl desired {"storage-control-rollback", 0, false, 1, 1};
		bool rejected = false;
		try
		{
			co_await store.updateDeliveryControl(desired, 0, "storage-test");
		}
		catch (const boost::system::system_error&)
		{
			rejected = true;
		}
		{
			auto session = co_await database->acquire();
			co_await session.query("DROP TRIGGER synthetic_control_audit");
		}
		require(rejected && !(co_await store.readDeliveryControl(desired.gameId)), "审计失败仍提交持久投递控制");
	}

	gamehub::Awaitable<void> verify(std::shared_ptr<gamehub::Database> database, gamehub::Config config)
	{
		using namespace gamehub;
		auto worker = config.worker;
		Store store(database);
		co_await store.checkSchema();
		Submission submission {"demo",
							   "announce",
							   "admin",
							   "storage-fencing",
							   "{\"synthetic\":true}",
							   "",
							   {1},
							   "storage-test"};
		submission.fingerprint = sha256(submission.payload);
		auto first = co_await store.accept(submission);
		auto second = co_await store.accept(submission);
		require(!first.duplicate && second.duplicate && first.eventId == second.eventId, "持久化幂等失败");
		auto different = submission;
		different.fingerprint = sha256("different");
		bool conflict = false;
		try
		{
			co_await store.accept(different);
		}
		catch (const ApiError& error)
		{
			conflict = error.status == 409;
		}
		require(conflict, "同键异载荷未被拒绝");

		auto claimed = co_await store.claim(worker);
		require(claimed.has_value(), "未领取任务");
		auto forged = *claimed;
		forged.generation += 1;
		require(!(co_await store.finish(forged, true, "", 0, worker.maxAttempts)), "错误代次确认成功");
		{
			auto session = co_await database->acquire();
			std::vector<std::string> key {std::to_string(claimed->id)};
			co_await session.query(
				"UPDATE delivery_task SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=?",
				key);
		}
		require(!(co_await store.finish(*claimed, true, "", 0, worker.maxAttempts)), "已过期但尚未回收的租约确认成功");
		require((co_await store.recover(worker)) == 1, "未回收过期租约");
		auto replacement = co_await store.claim(worker);
		require(replacement && replacement->id == claimed->id && replacement->generation > claimed->generation,
				"未重新领取同一任务");
		require(!(co_await store.finish(*claimed, true, "", 0, worker.maxAttempts)), "旧 Worker 覆盖新领取结果");
		require(co_await store.finish(*replacement, true, "", 0, worker.maxAttempts), "新 Worker 无法确认");

		submission.bizKey = "storage-rollback";
		{
			auto session = co_await database->acquire();
			co_await session.query("START TRANSACTION");
			std::vector<std::string> params {submission.gameId,
											 submission.taskType,
											 submission.nameSpace,
											 submission.bizKey,
											 submission.fingerprint,
											 submission.payload};
			co_await session.query(
				"INSERT INTO hub_event(game_id,task_type,namespace_key,biz_key,request_hash,payload,created_at) "
				"VALUES(?,?,?,?,?,?,UTC_TIMESTAMP(6))",
				std::move(params));
			// 不提交就退出作用域，模拟中途出错；归还连接后，池必须重置会话并回滚。
		}
		auto recovered = co_await store.accept(submission);
		require(!recovered.duplicate, "归还连接没有回滚未提交事务");
		worker.maxAttempts = 1;
		auto lastAttempt = co_await store.claim(worker);
		require(lastAttempt.has_value(), "未领取最终尝试");
		{
			auto session = co_await database->acquire();
			std::vector<std::string> key {std::to_string(lastAttempt->id)};
			co_await session.query(
				"UPDATE delivery_task SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE id=?",
				key);
		}
		require((co_await store.recover(worker)) == 1, "最终尝试崩溃没有回收");
		require(!(co_await store.claim(worker)), "超过最大尝试次数仍被自动领取");
		require(co_await store.replay("demo", lastAttempt->id, "storage-test"), "失败任务无法重排");
		auto replay = co_await store.claim(worker);
		require(replay && replay->attempts == 1, "人工重排未重置尝试次数");
		require(!(co_await store.finish(*lastAttempt, true, "", 0, 1)), "重排后旧代次仍有效");
		require(co_await store.finish(*replay, true, "", 0, 1), "重排任务无法完成");
		co_await verifyLoweredRetryLimit(store, worker);
		co_await verifyRecoverySkipsActiveLocks(database, store, worker);
		co_await verifyArchiveSkipsUnselectedLocks(database, store, worker);
		co_await verifyNonDeadlockIsNotRetried(database, store, worker);
		co_await verifyScopedClaims(store, worker);
		co_await verifyScopedRecovery(database, store, worker);
		co_await verifyScopedRecoveryBatches(database, store, worker);
		co_await verifyGameCounts(database, store);
		co_await verifyAttemptHistory(database, store, worker);
		co_await verifyAttemptRollback(database, store, worker);
		co_await verifyTaskFilters(store, worker);
		co_await verifyConfigState(store);
		co_await verifyConfigCanonicalBytes(store);
		co_await verifyDeliveryControl(store);
		co_await verifyControlAuditRollback(database, store);
		co_await verifyCancelledSession(std::move(config));
	}
} // namespace

int main(int argc, char** argv)
{
	if (argc != 2)
	{
		return 2;
	}
	try
	{
		auto config = gamehub::loadConfig(argv[1]);
		boost::asio::io_context context;
		auto database = std::make_shared<gamehub::Database>(context.get_executor(), config);
		boost::asio::co_spawn(context,
							  database->run(),
							  [](std::exception_ptr)
							  {
							  });
		std::exception_ptr failure;
		boost::asio::co_spawn(context,
							  verify(database, config),
							  [database, &failure](std::exception_ptr error)
							  {
								  database->stop();
								  failure = error;
							  });
		context.run();
		if (failure)
		{
			std::rethrow_exception(failure);
		}
		std::cout << "存储集成测试通过\n";
		return 0;
	}
	catch (const std::exception& error)
	{
		std::cerr << "存储集成测试失败：" << error.what() << '\n';
		return 1;
	}
}
