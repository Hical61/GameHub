/** @file Store.cpp
 * @brief 在短事务中领取任务，发 HTTP 时已释放数据库连接和行锁。
 */
#include "Store.h"
#include "core/Crypto.h"
#include <algorithm>
#include <array>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/this_coro.hpp>
#include <boost/asio/use_awaitable.hpp>
#include <boost/mysql/common_server_errc.hpp>
#include <boost/system/system_error.hpp>
#include <chrono>
#include <core/Log.h>

namespace gamehub
{
	namespace
	{
		constexpr std::size_t kMaxGameIdLength = 32;
		constexpr std::size_t kMaxExcludedServers = 1000;
		constexpr std::size_t kMaxScopedGames = 100;
		constexpr std::size_t kRecoveryBatchSize = 100;

		std::uint64_t number(boost::mysql::field_view value)
		{
			return value.is_uint64() ? value.as_uint64() : static_cast<std::uint64_t>(value.as_int64());
		}

		std::string text(boost::mysql::field_view value)
		{
			return std::string(value.as_string());
		}

		struct RowKeys
		{
			std::string placeholders;
			std::vector<std::string> params;
		};

		RowKeys rowKeys(const boost::mysql::results& result)
		{
			RowKeys keys;
			for (auto row : result.rows())
			{
				if (!keys.placeholders.empty())
				{
					keys.placeholders += ',';
				}
				keys.placeholders += '?';
				keys.params.push_back(std::to_string(number(row[0])));
			}
			return keys;
		}

		template <class T, class Operation>
		Awaitable<T> retryDeadlockedTransaction(std::string_view action, Operation operation)
		{
			constexpr unsigned kMaxAttempts = 3;
			constexpr auto kRetryPause = std::chrono::milliseconds(10);
			for (unsigned attempt = 1;; ++attempt)
			{
				try
				{
					co_return co_await operation();
				}
				catch (const boost::system::system_error& error)
				{
					if (error.code() != boost::mysql::common_server_errc::er_lock_deadlock)
					{
						throw;
					}
					if (attempt == kMaxAttempts)
					{
						HICAL_LOG_WARN("数据库死锁重试耗尽，action={} attempt={} category={} code={}",
									   action,
									   attempt,
									   error.code().category().name(),
									   error.code().value());
						throw;
					}
					HICAL_LOG_WARN("数据库死锁重试，action={} attempt={} category={} code={}",
								   action,
								   attempt,
								   error.code().category().name(),
								   error.code().value());
				}
				// operation 的协程已退出并释放会话；池重置后，下一次从 acquire 开始。
				// 只处理 InnoDB 已整笔回滚的 1213，取消、断连和未知提交结果不重试。
				boost::asio::steady_timer pause(co_await boost::asio::this_coro::executor);
				pause.expires_after(kRetryPause * attempt);
				co_await pause.async_wait(boost::asio::use_awaitable);
			}
		}

		void validateMaintenance(unsigned keepDays, unsigned batchSize)
		{
			if (keepDays < kMinRetentionDays || keepDays > kMaxRetentionDays || batchSize == 0
				|| batchSize > kMaxMaintenanceBatch)
			{
				throw ApiError(400, "保留天数或单批数量超出范围");
			}
		}

		std::string fixedDeliveryError(bool success, std::string error)
		{
			if (success)
			{
				return {};
			}
			constexpr std::array<std::string_view, 5> allowed {"network_error",
															   "network_timeout",
															   "invalid_ack",
															   "response_too_large",
															   "delivery_exception"};
			const bool httpStatus = error.size() == 8 && error.starts_with("http_") && error[5] >= '1'
									&& error[5] <= '5' && error[6] >= '0' && error[6] <= '9' && error[7] >= '0'
									&& error[7] <= '9';
			return httpStatus || std::find(allowed.begin(), allowed.end(), error) != allowed.end()
					   ? error
					   : "delivery_exception";
		}
	} // namespace

	Awaitable<void> Store::checkSchema()
	{
		auto session = co_await database_->acquire();
		auto result = co_await session.query("SELECT MAX(version) FROM schema_version");
		constexpr std::uint64_t kSchemaVersion = 4;
		if (result.rows().empty() || result.rows()[0][0].is_null() || number(result.rows()[0][0]) != kSchemaVersion)
		{
			throw std::runtime_error("数据库结构与当前程序不匹配，请执行配套迁移脚本");
		}
		co_await session.query("SELECT e.id,t.generation,c.version,a.id FROM hub_event e "
							   "LEFT JOIN delivery_task t ON t.event_id=e.id "
							   "LEFT JOIN config_head c ON c.game_id=e.game_id "
							   "LEFT JOIN audit_log a ON a.target_id=e.id LIMIT 0");
		co_await session.query("SELECT bucket_id,archived_count FROM delivery_task_counts LIMIT 0");
		co_await session.query(
			"SELECT id,payload_redacted_at,payload_version,fingerprint_version FROM hub_event LIMIT 0");
		co_await session.query("SELECT id FROM delivery_task_archive LIMIT 0");
		co_await session.query("SELECT game_id,bucket_id,pending_count FROM game_delivery_task_counts LIMIT 0");
		co_await session.query("SELECT task_id,generation,outcome FROM delivery_attempt LIMIT 0");
		co_await session.query("SELECT game_id,server_id,version,content_hash FROM config_server_state LIMIT 0");
		co_await session.query("SELECT game_id,version,enabled,max_concurrent_deliveries,max_concurrent_per_server "
							   "FROM game_delivery_control LIMIT 0");
	}

	Awaitable<void> Store::audit(DbSession& session,
								 std::string actor,
								 std::string action,
								 std::string gameId,
								 std::uint64_t targetId)
	{
		std::vector<std::string> params {std::move(actor),
										 std::move(action),
										 std::move(gameId),
										 std::to_string(targetId)};
		co_await session.query(
			"INSERT INTO audit_log(actor,action,game_id,target_id,created_at) VALUES(?,?,?,?,UTC_TIMESTAMP(6))",
			std::move(params));
	}

	Awaitable<AcceptResult> Store::insertEvent(DbSession& session, const Submission& submission)
	{
		if (submission.payloadVersion == 0 || submission.payloadVersion > kMaxContractVersion
			|| submission.fingerprintVersion == 0 || submission.fingerprintVersion > kMaxContractVersion)
		{
			throw ApiError(400, "业务载荷或摘要版本无效");
		}
		bool duplicate = false;
		std::uint64_t id = 0;
		std::vector<std::string> params {submission.gameId,
										 submission.taskType,
										 submission.nameSpace,
										 submission.bizKey,
										 submission.fingerprint,
										 submission.payload,
										 std::to_string(submission.payloadVersion),
										 std::to_string(submission.fingerprintVersion)};
		try
		{
			auto result = co_await session.query(
				"INSERT INTO hub_event(game_id,task_type,namespace_key,biz_key,request_hash,payload,payload_version,"
				"fingerprint_version,created_at) VALUES(?,?,?,?,?,?,?,?,UTC_TIMESTAMP(6))",
				std::move(params));
			id = result.last_insert_id();
		}
		catch (const boost::system::system_error& error)
		{
			if (error.code() != boost::mysql::common_server_errc::er_dup_entry)
			{
				throw;
			}
			duplicate = true;
		}
		if (duplicate)
		{
			std::vector<std::string> keys {submission.gameId,
										   submission.taskType,
										   submission.nameSpace,
										   submission.bizKey};
			// 事件提交后不再修改。重复 INSERT 已拿到共享记录锁，这里不再申请排他锁。
			// accept 到这里还没做过快照读；配置发布则先用 config_head 的行锁排队。
			auto result = co_await session.query(
				"SELECT id,request_hash,payload_version,fingerprint_version FROM hub_event WHERE game_id=? AND task_type=? "
				"AND namespace_key=? AND biz_key=?",
				std::move(keys));
			if (result.rows().empty() || text(result.rows()[0][1]) != submission.fingerprint
				|| number(result.rows()[0][2]) != submission.payloadVersion
				|| number(result.rows()[0][3]) != submission.fingerprintVersion)
			{
				throw ApiError(409, "相同业务键的内容不一致");
			}
			id = number(result.rows()[0][0]);
		}
		co_return AcceptResult {id, duplicate};
	}

	Awaitable<void> Store::insertTasks(DbSession& session, const Submission& submission, std::uint64_t eventId)
	{
		// 性能敏感：用一条 SQL 创建所有区服的任务，省去逐服执行的数据库往返。
		boost::json::array servers;
		for (auto serverId : submission.servers)
		{
			servers.push_back(serverId);
		}
		std::vector<std::string> params {std::to_string(eventId), submission.gameId, boost::json::serialize(servers)};
		co_await session.query(
			"INSERT INTO delivery_task(event_id,game_id,server_id,next_retry_at,created_at,updated_at) "
			"SELECT ?,?,j.server_id,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6) "
			"FROM JSON_TABLE(?, '$[*]' COLUMNS(server_id INT UNSIGNED PATH '$')) j",
			std::move(params));
		co_await audit(session, submission.actor, "accept_" + submission.taskType, submission.gameId, eventId);
	}

	Awaitable<AcceptResult> Store::accept(Submission submission)
	{
		// 闭包存在 helper 协程帧中；当前帧等待它结束，每次复制原参数给独立事务。
		co_return co_await retryDeadlockedTransaction<AcceptResult>("accept",
																	[this, &submission]()
																	{
																		return acceptOnce(submission);
																	});
	}

	Awaitable<AcceptResult> Store::acceptOnce(Submission submission)
	{
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		auto result = co_await insertEvent(session, submission);
		if (!result.duplicate)
		{
			co_await insertTasks(session, submission, result.eventId);
		}
		co_await session.query("COMMIT");
		co_return result;
	}

	Awaitable<std::optional<DeliveryTask>> Store::claim(const WorkerConfig& config)
	{
		co_return co_await retryDeadlockedTransaction<std::optional<DeliveryTask>>(
			"claim",
			[this, &config]()
			{
				return claimOnce(config, std::nullopt, {});
			});
	}

	Awaitable<std::optional<DeliveryTask>> Store::claim(const WorkerConfig& config,
														std::string gameId,
														std::vector<std::uint32_t> excludedServers)
	{
		if (!validKey(gameId, kMaxGameIdLength) || excludedServers.size() > kMaxExcludedServers)
		{
			throw std::invalid_argument("领取游戏或排除目标范围无效");
		}
		co_return co_await retryDeadlockedTransaction<std::optional<DeliveryTask>>(
			"claim",
			[this, &config, &gameId, &excludedServers]()
			{
				return claimOnce(config, gameId, excludedServers);
			});
	}

	Awaitable<std::optional<DeliveryTask>> Store::claimOnce(const WorkerConfig& config,
															std::optional<std::string> gameId,
															std::vector<std::uint32_t> excludedServers)
	{
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		// 性能敏感：游戏范围沿 idx_game_pending 领取；满额目标在 SQL 中排除，不先领取再退回。
		std::string sql = "SELECT id,event_id,game_id,server_id,attempts,generation,"
						  "TIMESTAMPDIFF(MICROSECOND,'1970-01-01 00:00:00',created_at) FROM delivery_task ";
		sql += gameId ? "FORCE INDEX(idx_game_pending) WHERE game_id=? AND " : "WHERE ";
		sql += "status=0 AND next_retry_at<=UTC_TIMESTAMP(6) ";
		std::vector<std::string> params;
		if (gameId)
		{
			params.push_back(*gameId);
		}
		if (!excludedServers.empty())
		{
			sql += "AND server_id NOT IN (";
			for (auto serverId : excludedServers)
			{
				sql += params.size() > (gameId ? 1U : 0U) ? ",?" : "?";
				params.push_back(std::to_string(serverId));
			}
			sql += ") ";
		}
		sql += "ORDER BY next_retry_at,id LIMIT 1 FOR UPDATE SKIP LOCKED";
		auto result = co_await session.query(std::move(sql), std::move(params));
		if (result.rows().empty())
		{
			co_await session.query("COMMIT");
			co_return std::nullopt;
		}
		const auto row = result.rows()[0];
		if (number(row[4]) >= config.maxAttempts)
		{
			// 重试上限调低后，旧 pending 可能已用完次数，先转 failed，人工核对后再重排。
			std::vector<std::string> key {std::to_string(number(row[0]))};
			co_await session.query("UPDATE delivery_task SET status=3,generation=generation+1,lease_until=NULL,"
								   "last_error='attempts_exhausted',updated_at=UTC_TIMESTAMP(6) WHERE id=?",
								   std::move(key));
			co_await session.query("COMMIT");
			co_return std::nullopt;
		}
		DeliveryTask task;
		task.id = number(row[0]);
		task.gameId = text(row[2]);
		task.serverId = static_cast<std::uint32_t>(number(row[3]));
		task.attempts = static_cast<unsigned>(number(row[4])) + 1;
		task.generation = number(row[5]) + 1;
		task.eventId = number(row[1]);
		task.createdAtUnixMicros = row[6].as_int64();
		std::vector<std::string> eventKey {std::to_string(task.eventId)};
		auto event = co_await session.query(
			"SELECT task_type,namespace_key,biz_key,payload,payload_version,fingerprint_version FROM hub_event WHERE id=?",
			std::move(eventKey));
		task.taskType = text(event.rows()[0][0]);
		task.nameSpace = text(event.rows()[0][1]);
		task.bizKey = text(event.rows()[0][2]);
		task.payload = text(event.rows()[0][3]);
		task.payloadVersion = static_cast<unsigned>(number(event.rows()[0][4]));
		task.fingerprintVersion = static_cast<unsigned>(number(event.rows()[0][5]));
		std::vector<std::string> update {std::to_string(config.leaseSeconds), std::to_string(task.id)};
		co_await session.query(
			"UPDATE delivery_task SET status=1,attempts=attempts+1,generation=generation+1,"
			"lease_until=TIMESTAMPADD(SECOND,?,UTC_TIMESTAMP(6)),updated_at=UTC_TIMESTAMP(6) WHERE id=?",
			std::move(update));
		std::vector<std::string> attempt {std::to_string(task.id),
										  std::to_string(task.eventId),
										  task.gameId,
										  std::to_string(task.serverId),
										  std::to_string(task.generation),
										  std::to_string(task.attempts)};
		co_await session.query("INSERT INTO delivery_attempt(task_id,event_id,game_id,server_id,generation,"
							   "attempt_number,started_at) VALUES(?,?,?,?,?,?,UTC_TIMESTAMP(6))",
							   std::move(attempt));
		co_await session.query("COMMIT");
		co_return task;
	}

	Awaitable<std::uint64_t> Store::recover(const WorkerConfig& config)
	{
		co_return co_await retryDeadlockedTransaction<std::uint64_t>("recover",
																	 [this, &config]()
																	 {
																		 return recoverOnce(config, {});
																	 });
	}

	Awaitable<std::uint64_t> Store::recover(const WorkerConfig& config, std::vector<std::string> gameIds)
	{
		if (gameIds.empty())
		{
			co_return 0;
		}
		if (gameIds.size() > kMaxScopedGames)
		{
			throw std::invalid_argument("回收游戏范围过大");
		}
		for (const auto& gameId : gameIds)
		{
			if (!validKey(gameId, kMaxGameIdLength))
			{
				throw std::invalid_argument("回收游戏标识无效");
			}
		}
		co_return co_await retryDeadlockedTransaction<std::uint64_t>("recover",
																	 [this, &config, &gameIds]()
																	 {
																		 return recoverOnce(config, gameIds);
																	 });
	}

	Awaitable<std::uint64_t> Store::recoverOnce(const WorkerConfig& config, std::vector<std::string> gameIds)
	{
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		// 先跳过正在确认的行，只更新本事务已经锁定的过期任务，保持 finish 的主键锁顺序。
		std::string sql;
		if (gameIds.empty())
		{
			sql = "SELECT id FROM delivery_task WHERE status=1 AND lease_until<=UTC_TIMESTAMP(6) "
				  "ORDER BY lease_until,id LIMIT "
				  + std::to_string(kRecoveryBatchSize) + " FOR UPDATE SKIP LOCKED";
		}
		else
		{
			// 每个分支固定一个 game_id，沿游戏租约索引有序取数；多游戏 IN 后全局排序会锁住整个积压。
			// 各游戏预分配总计 100 条配额，避免前一游戏持续积压时后续游戏一直不能回收。
			for (std::size_t i = 0; i < gameIds.size(); ++i)
			{
				if (i != 0)
				{
					sql += " UNION ALL ";
				}
				const auto quota = kRecoveryBatchSize / gameIds.size() + (i < kRecoveryBatchSize % gameIds.size());
				if (gameIds.size() > 1)
				{
					sql += '(';
				}
				sql += "SELECT id FROM delivery_task FORCE INDEX(idx_game_lease) WHERE game_id=? "
					   "AND status=1 AND lease_until<=UTC_TIMESTAMP(6) ORDER BY lease_until,id LIMIT "
					   + std::to_string(quota) + " FOR UPDATE SKIP LOCKED";
				if (gameIds.size() > 1)
				{
					sql += ')';
				}
			}
		}
		auto selected = co_await session.query(std::move(sql), gameIds);
		if (selected.rows().empty())
		{
			co_await session.query("COMMIT");
			co_return 0;
		}
		auto keys = rowKeys(selected);
		// 与 claim/finish 一样先持有任务锁，再更新它的尝试；尝试记录不存在时仍按租约回收任务。
		co_await session.query("UPDATE delivery_attempt a JOIN delivery_task t ON t.id=a.task_id "
							   "AND t.generation=a.generation SET a.finished_at=UTC_TIMESTAMP(6),"
							   "a.outcome='lease_expired',a.error_category='lease_expired' WHERE t.id IN ("
								   + keys.placeholders + ") AND a.finished_at IS NULL",
							   keys.params);
		keys.params.insert(keys.params.begin(), std::to_string(config.maxAttempts));
		auto result = co_await session.query(
			"UPDATE delivery_task FORCE INDEX(PRIMARY) SET status=IF(attempts>=?,3,0),generation=generation+1,"
			"lease_until=NULL,next_retry_at=UTC_TIMESTAMP(6),last_error='lease_expired',updated_at=UTC_TIMESTAMP(6) "
			"WHERE id IN ("
				+ keys.placeholders + ") AND status=1 AND lease_until<=UTC_TIMESTAMP(6)",
			std::move(keys.params));
		co_await session.query("COMMIT");
		co_return result.affected_rows();
	}

	Awaitable<bool> Store::finish(const DeliveryTask& task,
								  bool success,
								  std::string error,
								  unsigned delay,
								  unsigned maxAttempts)
	{
		co_return co_await retryDeadlockedTransaction<bool>(
			"finish",
			[this, &task, success, &error, delay, maxAttempts]()
			{
				return finishOnce(task, success, error, delay, maxAttempts);
			});
	}

	Awaitable<bool> Store::finishOnce(const DeliveryTask& task,
									  bool success,
									  std::string error,
									  unsigned delay,
									  unsigned maxAttempts)
	{
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		const auto status =
			success ? TaskStatus::Done : (task.attempts >= maxAttempts ? TaskStatus::Failed : TaskStatus::Pending);
		error = fixedDeliveryError(success, std::move(error));
		std::vector<std::string> params {std::to_string(static_cast<int>(status)),
										 std::to_string(delay),
										 error,
										 std::to_string(task.id),
										 std::to_string(task.generation)};
		auto result = co_await session.query(
			"UPDATE delivery_task SET status=?,lease_until=NULL,"
			"next_retry_at=TIMESTAMPADD(SECOND,?,UTC_TIMESTAMP(6)),last_error=?,updated_at=UTC_TIMESTAMP(6) "
			"WHERE id=? AND generation=? AND status=1 AND lease_until>UTC_TIMESTAMP(6)",
			std::move(params));
		const bool updated = result.affected_rows() == 1;
		if (updated)
		{
			std::vector<std::string> attempt {success ? "success" : "failure",
											  error,
											  std::to_string(task.id),
											  std::to_string(task.generation)};
			co_await session.query(
				"UPDATE delivery_attempt SET finished_at=UTC_TIMESTAMP(6),outcome=?,error_category=? "
				"WHERE task_id=? AND generation=? AND finished_at IS NULL",
				std::move(attempt));
		}
		co_await session.query("COMMIT");
		co_return updated;
	}

	Awaitable<boost::json::array> Store::listTasks(std::string gameId, std::uint64_t after)
	{
		auto session = co_await database_->acquire();
		std::vector<std::string> params {std::move(gameId), std::to_string(after)};
		auto result = co_await session.query("SELECT id,event_id,server_id,status,attempts,generation,last_error "
											 "FROM delivery_task WHERE game_id=? AND id>? ORDER BY id LIMIT 100",
											 std::move(params));
		boost::json::array items;
		for (auto row : result.rows())
		{
			items.push_back(boost::json::object {{"id", std::to_string(number(row[0]))},
												 {"eventId", std::to_string(number(row[1]))},
												 {"serverId", number(row[2])},
												 {"status", number(row[3])},
												 {"attempts", number(row[4])},
												 {"generation", std::to_string(number(row[5]))},
												 {"lastError", text(row[6])}});
		}
		co_return items;
	}

	Awaitable<bool> Store::replay(std::string gameId, std::uint64_t taskId, std::string actor)
	{
		co_return co_await retryDeadlockedTransaction<bool>("replay",
															[this, &gameId, taskId, &actor]()
															{
																return replayOnce(gameId, taskId, actor);
															});
	}

	Awaitable<bool> Store::replayOnce(std::string gameId, std::uint64_t taskId, std::string actor)
	{
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		std::vector<std::string> params {gameId, std::to_string(taskId)};
		auto result = co_await session.query(
			"UPDATE delivery_task SET status=0,attempts=0,generation=generation+1,"
			"next_retry_at=UTC_TIMESTAMP(6),lease_until=NULL,last_error='',updated_at=UTC_TIMESTAMP(6) "
			"WHERE game_id=? AND id=? AND status=3",
			std::move(params));
		if (result.affected_rows() == 1)
		{
			co_await audit(session, std::move(actor), "replay_task", gameId, taskId);
		}
		co_await session.query("COMMIT");
		co_return result.affected_rows() == 1;
	}

	Awaitable<boost::json::array> Store::listArchived(std::string gameId, std::uint64_t after)
	{
		auto session = co_await database_->acquire();
		std::vector<std::string> params {std::move(gameId), std::to_string(after)};
		auto result =
			co_await session.query("SELECT id,event_id,server_id,attempts,generation FROM delivery_task_archive "
								   "WHERE game_id=? AND id>? ORDER BY id LIMIT 100",
								   std::move(params));
		boost::json::array items;
		for (auto row : result.rows())
		{
			items.push_back(boost::json::object {{"id", std::to_string(number(row[0]))},
												 {"eventId", std::to_string(number(row[1]))},
												 {"serverId", number(row[2])},
												 {"status", static_cast<unsigned>(TaskStatus::Done)},
												 {"attempts", number(row[3])},
												 {"generation", std::to_string(number(row[4]))}});
		}
		co_return items;
	}

	Awaitable<boost::json::object> Store::stats()
	{
		auto session = co_await database_->acquire();
		// 性能敏感：历史数据不参与扫描；最老 pending 走 (status,created_at,id) 索引。
		auto result = co_await session.query(
			"SELECT CAST(SUM(pending_count) AS UNSIGNED),CAST(SUM(delivering_count) AS UNSIGNED),"
			"CAST(SUM(done_count) AS UNSIGNED),CAST(SUM(failed_count) AS UNSIGNED),"
			"CAST(SUM(archived_count) AS UNSIGNED),"
			"(SELECT GREATEST(0,COALESCE(TIMESTAMPDIFF(SECOND,MIN(created_at),UTC_TIMESTAMP(6)),0)) "
			"FROM delivery_task FORCE INDEX(idx_status_created) WHERE status=0) FROM delivery_task_counts");
		const auto row = result.rows()[0];
		co_return boost::json::object {{"pending", number(row[0])},
									   {"delivering", number(row[1])},
									   {"done", number(row[2])},
									   {"failed", number(row[3])},
									   {"archivedDone", number(row[4])},
									   {"completedTotal", number(row[2]) + number(row[4])},
									   {"oldestPendingSeconds", number(row[5])}};
	}

	Awaitable<boost::json::object> Store::stats(std::string gameId)
	{
		if (!validKey(gameId, kMaxGameIdLength))
		{
			throw std::invalid_argument("统计游戏标识无效");
		}
		auto session = co_await database_->acquire();
		// 性能敏感：每款游戏最多 64 行计数，最老 pending 使用游戏范围索引，不扫描历史任务。
		std::vector<std::string> params {gameId, gameId};
		auto result = co_await session.query(
			"SELECT CAST(COALESCE(SUM(pending_count),0) AS UNSIGNED),"
			"CAST(COALESCE(SUM(delivering_count),0) AS UNSIGNED),"
			"CAST(COALESCE(SUM(done_count),0) AS UNSIGNED),CAST(COALESCE(SUM(failed_count),0) AS UNSIGNED),"
			"CAST(COALESCE(SUM(archived_count),0) AS UNSIGNED),"
			"(SELECT GREATEST(0,COALESCE(TIMESTAMPDIFF(SECOND,MIN(created_at),UTC_TIMESTAMP(6)),0)) "
			"FROM delivery_task FORCE INDEX(idx_game_status_created) WHERE game_id=? AND status=0) "
			"FROM game_delivery_task_counts WHERE game_id=?",
			std::move(params));
		const auto row = result.rows()[0];
		co_return boost::json::object {{"pending", number(row[0])},
									   {"delivering", number(row[1])},
									   {"done", number(row[2])},
									   {"failed", number(row[3])},
									   {"archivedDone", number(row[4])},
									   {"completedTotal", number(row[2]) + number(row[4])},
									   {"oldestPendingSeconds", number(row[5])}};
	}

	Awaitable<std::uint64_t> Store::archiveDone(std::string gameId,
												unsigned keepDays,
												unsigned batchSize,
												std::string actor)
	{
		co_return co_await retryDeadlockedTransaction<std::uint64_t>(
			"archive",
			[this, &gameId, keepDays, batchSize, &actor]()
			{
				return archiveDoneOnce(gameId, keepDays, batchSize, actor);
			});
	}

	Awaitable<std::uint64_t> Store::archiveDoneOnce(std::string gameId,
													unsigned keepDays,
													unsigned batchSize,
													std::string actor)
	{
		validateMaintenance(keepDays, batchSize);
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		std::vector<std::string> bounds {gameId, std::to_string(keepDays)};
		auto selected =
			co_await session.query("SELECT id FROM delivery_task WHERE game_id=? AND status=2 "
								   "AND updated_at<=TIMESTAMPADD(DAY,-?,UTC_TIMESTAMP(6)) ORDER BY updated_at,id LIMIT "
									   + std::to_string(batchSize) + " FOR UPDATE SKIP LOCKED",
								   std::move(bounds));
		if (selected.rows().empty())
		{
			co_await session.query("COMMIT");
			co_return 0;
		}
		auto keys = rowKeys(selected);
		co_await session.query(
			"INSERT INTO delivery_task_archive SELECT t.*,UTC_TIMESTAMP(6) FROM delivery_task t FORCE INDEX(PRIMARY) "
			"WHERE id IN ("
				+ keys.placeholders + ")",
			keys.params);
		auto removed = co_await session.query("DELETE t FROM delivery_task t FORCE INDEX(PRIMARY) WHERE id IN ("
												  + keys.placeholders + ")",
											  std::move(keys.params));
		co_await audit(session, std::move(actor), "archive_done", gameId, number(selected.rows().back()[0]));
		co_await session.query("COMMIT");
		co_return removed.affected_rows();
	}

	Awaitable<std::uint64_t> Store::redactCompleted(std::string gameId,
													unsigned keepDays,
													unsigned batchSize,
													std::string actor)
	{
		co_return co_await retryDeadlockedTransaction<std::uint64_t>(
			"redact",
			[this, &gameId, keepDays, batchSize, &actor]()
			{
				return redactCompletedOnce(gameId, keepDays, batchSize, actor);
			});
	}

	Awaitable<std::uint64_t> Store::redactCompletedOnce(std::string gameId,
														unsigned keepDays,
														unsigned batchSize,
														std::string actor)
	{
		validateMaintenance(keepDays, batchSize);
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		std::vector<std::string> bounds {gameId, std::to_string(keepDays)};
		auto selected =
			co_await session.query("SELECT e.id FROM hub_event e WHERE e.game_id=? AND e.payload_redacted_at IS NULL "
								   "AND e.created_at<=TIMESTAMPADD(DAY,-?,UTC_TIMESTAMP(6)) "
								   "AND EXISTS(SELECT 1 FROM delivery_task_archive a WHERE a.event_id=e.id) "
								   "AND NOT EXISTS(SELECT 1 FROM delivery_task t WHERE t.event_id=e.id) "
								   "ORDER BY e.created_at,e.id LIMIT "
									   + std::to_string(batchSize) + " FOR UPDATE SKIP LOCKED",
								   std::move(bounds));
		if (selected.rows().empty())
		{
			co_await session.query("COMMIT");
			co_return 0;
		}
		// 先提交归档，再单独清载荷，避免多个归档批次持有同一事件的共享外键锁后互相升级。
		auto keys = rowKeys(selected);
		auto updated = co_await session.query(
			"UPDATE hub_event FORCE INDEX(PRIMARY) SET payload=JSON_OBJECT(),payload_redacted_at=UTC_TIMESTAMP(6) "
			"WHERE id IN ("
				+ keys.placeholders + ")",
			std::move(keys.params));
		co_await audit(session, std::move(actor), "redact_payload", gameId, number(selected.rows().back()[0]));
		co_await session.query("COMMIT");
		co_return updated.affected_rows();
	}

	Awaitable<AcceptResult> Store::publishConfig(Submission submission, std::uint64_t version, std::string content)
	{
		co_return co_await retryDeadlockedTransaction<AcceptResult>(
			"publish",
			[this, &submission, version, &content]()
			{
				return publishConfigOnce(submission, version, content);
			});
	}

	Awaitable<AcceptResult> Store::publishConfigOnce(Submission submission, std::uint64_t version, std::string content)
	{
		auto session = co_await database_->acquire();
		co_await session.query("START TRANSACTION");
		std::vector<std::string> gameKey {submission.gameId};
		co_await session.query(
			"INSERT INTO config_head(game_id,version) VALUES(?,0) ON DUPLICATE KEY UPDATE game_id=game_id",
			gameKey);
		auto head = co_await session.query("SELECT version FROM config_head WHERE game_id=? FOR UPDATE", gameKey);
		const auto latest = number(head.rows()[0][0]);
		if (version <= latest)
		{
			std::vector<std::string> keys {submission.gameId, std::to_string(version)};
			auto existing =
				co_await session.query("SELECT content_hash FROM config_version WHERE game_id=? AND version=?",
									   std::move(keys));
			if (existing.rows().empty() || text(existing.rows()[0][0]) != sha256(content))
			{
				throw ApiError(409, "新版本号必须更大，已发布的内容不能修改");
			}
		}
		else
		{
			std::vector<std::string> params {submission.gameId, std::to_string(version), content, sha256(content)};
			co_await session.query("INSERT INTO config_version(game_id,version,content,content_hash,created_at) "
								   "VALUES(?,?,?,?,UTC_TIMESTAMP(6))",
								   std::move(params));
			std::vector<std::string> update {std::to_string(version), submission.gameId};
			co_await session.query("UPDATE config_head SET version=? WHERE game_id=?", std::move(update));
		}
		auto result = co_await insertEvent(session, submission);
		if (!result.duplicate)
		{
			co_await insertTasks(session, submission, result.eventId);
		}
		co_await session.query("COMMIT");
		co_return result;
	}

	Awaitable<boost::json::object> Store::readConfig(std::string gameId, std::uint64_t version)
	{
		auto session = co_await database_->acquire();
		std::vector<std::string> params {gameId, std::to_string(version), gameId, std::to_string(version)};
		auto result = co_await session.query("SELECT version,content,content_hash FROM config_version WHERE game_id=? "
											 "AND version=IF(?=0,(SELECT version FROM config_head WHERE game_id=?),?)",
											 std::move(params));
		if (result.rows().empty())
		{
			throw ApiError(404, "配置版本不存在");
		}
		auto row = result.rows()[0];
		auto content = boost::json::parse(text(row[1]));
		const auto canonicalContent = canonicalJson(content);
		co_return boost::json::object {{"version", number(row[0])},
									   {"content", std::move(content)},
									   {"canonicalContent", canonicalContent},
									   {"sha256", text(row[2])}};
	}

	Awaitable<boost::json::array> Store::listAudit(std::string gameId, std::uint64_t after)
	{
		auto session = co_await database_->acquire();
		std::vector<std::string> params {std::move(gameId), std::to_string(after)};
		auto result = co_await session.query("SELECT id,actor,action,target_id FROM audit_log "
											 "WHERE game_id=? AND id>? ORDER BY id LIMIT 100",
											 std::move(params));
		boost::json::array items;
		for (auto row : result.rows())
		{
			items.push_back(boost::json::object {{"id", std::to_string(number(row[0]))},
												 {"actor", text(row[1])},
												 {"action", text(row[2])},
												 {"targetId", std::to_string(number(row[3]))}});
		}
		co_return items;
	}
} // namespace gamehub
