/** @file storage_tests.cpp
 * @brief 用集成脚本创建的临时库测试租约隔离和异常回滚。
 */
#include "core/Crypto.h"
#include "storage/Store.h"
#include <boost/asio/cancel_after.hpp>
#include <boost/asio/co_spawn.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/asio/this_coro.hpp>
#include <boost/asio/use_future.hpp>
#include <boost/mysql/common_server_errc.hpp>
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
