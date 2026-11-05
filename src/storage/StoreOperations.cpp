/** @file StoreOperations.cpp
 * @brief 业务身份检索、逐服配置状态和带版本比较的持久投递控制。
 */
#include "Store.h"
#include <algorithm>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/this_coro.hpp>
#include <boost/asio/use_awaitable.hpp>
#include <boost/mysql/common_server_errc.hpp>
#include <boost/system/system_error.hpp>
#include <chrono>
#include <core/Log.h>
#include <limits>
#include <set>

namespace gamehub
{
	namespace
	{
		constexpr unsigned kMaxPageSize = 100;
		constexpr std::size_t kMaxEventTargets = 1000;
		constexpr std::size_t kMaxGames = 100;
		constexpr unsigned kMaxDeliveryConcurrency = 32;
		constexpr std::int64_t kMaxUnixSeconds = 253402300799;

		std::uint64_t integer(boost::mysql::field_view value)
		{
			return value.is_uint64() ? value.as_uint64() : static_cast<std::uint64_t>(value.as_int64());
		}

		std::string string(boost::mysql::field_view value)
		{
			return std::string(value.as_string());
		}

		void validateGame(std::string_view gameId)
		{
			if (!validKey(gameId, 32))
			{
				throw ApiError(400, "游戏标识无效");
			}
		}

		void validatePage(unsigned limit)
		{
			if (limit == 0 || limit > kMaxPageSize)
			{
				throw ApiError(400, "分页数量必须在 1 到 100 之间");
			}
		}

		template <class T, class Operation>
		Awaitable<T> retryOperation(std::string_view action, Operation operation)
		{
			constexpr unsigned kMaxAttempts = 3;
			for (unsigned attempt = 1;; ++attempt)
			{
				try
				{
					co_return co_await operation();
				}
				catch (const boost::system::system_error& error)
				{
					if (error.code() != boost::mysql::common_server_errc::er_lock_deadlock || attempt == kMaxAttempts)
					{
						throw;
					}
					HICAL_LOG_WARN("数据库死锁重试，action={} attempt={} category={} code={}",
								   action,
								   attempt,
								   error.code().category().name(),
								   error.code().value());
				}
				// 上次操作退出后会话已经归还；仅重试已由 InnoDB 整笔回滚的 1213。
				boost::asio::steady_timer pause(co_await boost::asio::this_coro::executor);
				pause.expires_after(std::chrono::milliseconds(10 * attempt));
				co_await pause.async_wait(boost::asio::use_awaitable);
			}
		}

		boost::json::object taskMetadata(boost::mysql::row_view row)
		{
			return {
				{"id", std::to_string(integer(row[0]))},
				{"eventId", std::to_string(integer(row[1]))},
				{"serverId", integer(row[2])},
				{"status", integer(row[3])},
				{"attempts", integer(row[4])},
				{"generation", std::to_string(integer(row[5]))},
				{"lastError", string(row[6])},
				{"createdAt", string(row[7])},
				{"updatedAt", string(row[8])},
				{"nextRetryAt", string(row[9])},
				{"leaseUntil", row[10].is_null() ? boost::json::value(nullptr) : boost::json::value(string(row[10]))},
				{"archived", integer(row[11]) != 0}};
		}

		constexpr std::string_view kTaskColumns =
			"id,event_id,server_id,status,attempts,generation,last_error,CAST(created_at AS CHAR),"
			"CAST(updated_at AS CHAR),CAST(next_retry_at AS CHAR),CAST(lease_until AS CHAR)";

		DeliveryControl controlFromRow(boost::mysql::row_view row)
		{
			return {string(row[0]),
					integer(row[1]),
					integer(row[2]) != 0,
					static_cast<unsigned>(integer(row[3])),
					static_cast<unsigned>(integer(row[4]))};
		}

		bool validHash(std::string_view hash)
		{
			return hash.size() == 64
				   && std::all_of(hash.begin(),
								  hash.end(),
								  [](char value)
								  {
									  return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f');
								  });
		}
	} // namespace

	Awaitable<boost::json::array> Store::listTasks(std::string gameId, TaskFilter filter)
	{
		validateGame(gameId);
		validatePage(filter.limit);
		const auto validTime = [](const auto& value)
		{
			return !value || (*value >= 0 && *value <= kMaxUnixSeconds);
		};
		if ((filter.status && static_cast<unsigned>(*filter.status) > static_cast<unsigned>(TaskStatus::Failed))
			|| (filter.serverId && *filter.serverId == 0) || !validTime(filter.createdFrom)
			|| !validTime(filter.createdBefore)
			|| (filter.createdFrom && filter.createdBefore && *filter.createdFrom >= *filter.createdBefore))
		{
			throw ApiError(400, "任务筛选范围无效");
		}
		std::string sql = "SELECT " + std::string(kTaskColumns) + ",0 FROM delivery_task WHERE game_id=? AND id>?";
		std::vector<std::string> params {std::move(gameId), std::to_string(filter.after)};
		if (filter.status)
		{
			sql += " AND status=?";
			params.push_back(std::to_string(static_cast<int>(*filter.status)));
		}
		if (filter.serverId)
		{
			sql += " AND server_id=?";
			params.push_back(std::to_string(*filter.serverId));
		}
		if (filter.createdFrom)
		{
			sql += " AND created_at>=TIMESTAMPADD(SECOND,?,'1970-01-01 00:00:00')";
			params.push_back(std::to_string(*filter.createdFrom));
		}
		if (filter.createdBefore)
		{
			sql += " AND created_at<TIMESTAMPADD(SECOND,?,'1970-01-01 00:00:00')";
			params.push_back(std::to_string(*filter.createdBefore));
		}
		sql += " ORDER BY id LIMIT " + std::to_string(filter.limit);
		auto session = co_await database_->acquire();
		auto result = co_await session.query(std::move(sql), std::move(params));
		boost::json::array items;
		for (auto row : result.rows())
		{
			items.push_back(taskMetadata(row));
		}
		co_return items;
	}

	Awaitable<boost::json::array> Store::gameStats(std::vector<std::string> gameIds)
	{
		if (gameIds.empty())
		{
			co_return boost::json::array {};
		}
		if (gameIds.size() > kMaxGames)
		{
			throw ApiError(400, "游戏统计范围过大");
		}
		std::set<std::string> unique;
		boost::json::array games;
		for (const auto& gameId : gameIds)
		{
			validateGame(gameId);
			if (!unique.insert(gameId).second)
			{
				throw ApiError(400, "游戏统计范围不能重复");
			}
			games.emplace_back(gameId);
		}
		auto session = co_await database_->acquire();
		std::vector<std::string> params {boost::json::serialize(games)};
		// 性能敏感：计数最多每游戏 64 行，相关最老时间查找走游戏/状态/时间索引，不逐游戏往返。
		auto result = co_await session.query(
			"SELECT g.game_id,CAST(COALESCE(SUM(c.pending_count),0) AS UNSIGNED),"
			"CAST(COALESCE(SUM(c.delivering_count),0) AS UNSIGNED),CAST(COALESCE(SUM(c.done_count),0) AS UNSIGNED),"
			"CAST(COALESCE(SUM(c.failed_count),0) AS UNSIGNED),CAST(COALESCE(SUM(c.archived_count),0) AS UNSIGNED),"
			"(SELECT GREATEST(0,COALESCE(TIMESTAMPDIFF(SECOND,MIN(t.created_at),UTC_TIMESTAMP(6)),0)) "
			"FROM delivery_task t FORCE INDEX(idx_game_status_created) WHERE t.game_id=g.game_id AND t.status=0) "
			"FROM JSON_TABLE(?, '$[*]' COLUMNS(game_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin PATH '$')) g "
			"LEFT JOIN game_delivery_task_counts c ON c.game_id=g.game_id GROUP BY g.game_id ORDER BY g.game_id",
			std::move(params));
		boost::json::array items;
		for (auto row : result.rows())
		{
			items.push_back(boost::json::object {{"gameId", string(row[0])},
												 {"pending", integer(row[1])},
												 {"delivering", integer(row[2])},
												 {"done", integer(row[3])},
												 {"failed", integer(row[4])},
												 {"archivedDone", integer(row[5])},
												 {"oldestPendingSeconds", integer(row[6])}});
		}
		co_return items;
	}

	Awaitable<boost::json::object> Store::lookupEvent(std::string gameId,
													  std::string taskType,
													  std::string nameSpace,
													  std::string bizKey)
	{
		validateGame(gameId);
		if (!validKey(taskType, 32) || !validKey(nameSpace, 32) || !validKey(bizKey))
		{
			throw ApiError(400, "业务身份无效");
		}
		auto session = co_await database_->acquire();
		std::vector<std::string> keys {std::move(gameId), std::move(taskType), std::move(nameSpace), std::move(bizKey)};
		auto event = co_await session.query(
			"SELECT id,game_id,task_type,namespace_key,biz_key,request_hash,"
			"payload_version,fingerprint_version,CAST(created_at AS CHAR),payload_redacted_at IS NOT NULL "
			"FROM hub_event WHERE game_id=? AND task_type=? AND namespace_key=? AND biz_key=?",
			std::move(keys));
		if (event.rows().empty())
		{
			throw ApiError(404, "事件不存在");
		}
		const auto row = event.rows()[0];
		const auto id = std::to_string(integer(row[0]));
		boost::json::object result {{"id", id},
									{"gameId", string(row[1])},
									{"taskType", string(row[2])},
									{"namespace", string(row[3])},
									{"bizKey", string(row[4])},
									{"requestHash", string(row[5])},
									{"payloadVersion", integer(row[6])},
									{"fingerprintVersion", integer(row[7])},
									{"createdAt", string(row[8])},
									{"payloadRedacted", integer(row[9]) != 0}};
		std::vector<std::string> eventKeys {id, id};
		auto tasks = co_await session.query(
			"SELECT " + std::string(kTaskColumns) + ",0 FROM delivery_task WHERE event_id=? UNION ALL SELECT "
				+ std::string(kTaskColumns) + ",1 FROM delivery_task_archive WHERE event_id=? ORDER BY id LIMIT "
				+ std::to_string(kMaxEventTargets + 1),
			std::move(eventKeys));
		if (tasks.rows().size() > kMaxEventTargets)
		{
			throw ApiError(413, "事件任务数量超过查询上限");
		}
		boost::json::array items;
		for (auto task : tasks.rows())
		{
			items.push_back(taskMetadata(task));
		}
		result["tasks"] = std::move(items);
		co_return result;
	}

	Awaitable<boost::json::array> Store::listAttempts(std::string gameId,
													  std::uint64_t taskId,
													  std::uint64_t after,
													  unsigned limit)
	{
		validateGame(gameId);
		validatePage(limit);
		if (taskId == 0)
		{
			throw ApiError(400, "任务编号无效");
		}
		auto session = co_await database_->acquire();
		std::vector<std::string> keys {gameId, std::to_string(taskId), gameId, std::to_string(taskId)};
		auto task =
			co_await session.query("SELECT id FROM delivery_task WHERE game_id=? AND id=? "
								   "UNION ALL SELECT id FROM delivery_task_archive WHERE game_id=? AND id=? LIMIT 1",
								   std::move(keys));
		if (task.rows().empty())
		{
			throw ApiError(404, "任务不存在");
		}
		std::vector<std::string> params {std::move(gameId), std::to_string(taskId), std::to_string(after)};
		auto attempts = co_await session.query("SELECT id,generation,attempt_number,CAST(started_at AS CHAR),"
											   "CAST(finished_at AS CHAR),outcome,error_category FROM delivery_attempt "
											   "WHERE game_id=? AND task_id=? AND id>? ORDER BY id LIMIT "
												   + std::to_string(limit),
											   std::move(params));
		boost::json::array items;
		for (auto row : attempts.rows())
		{
			items.push_back(boost::json::object {
				{"id", std::to_string(integer(row[0]))},
				{"generation", std::to_string(integer(row[1]))},
				{"attemptNumber", integer(row[2])},
				{"startedAt", string(row[3])},
				{"finishedAt", row[4].is_null() ? boost::json::value(nullptr) : boost::json::value(string(row[4]))},
				{"outcome", string(row[5])},
				{"errorCategory", string(row[6])}});
		}
		co_return items;
	}

	Awaitable<boost::json::object> Store::reportConfigApplied(std::string gameId,
															  std::uint32_t serverId,
															  std::uint64_t version,
															  std::string contentHash)
	{
		validateGame(gameId);
		if (serverId == 0 || version == 0 || !validHash(contentHash))
		{
			throw ApiError(400, "配置应用报告无效");
		}
		co_return co_await retryOperation<boost::json::object>(
			"config_applied",
			[this, &gameId, serverId, version, &contentHash]() -> Awaitable<boost::json::object>
			{
				auto session = co_await database_->acquire();
				co_await session.query("START TRANSACTION");
				std::vector<std::string> configKey {gameId, std::to_string(version)};
				auto config =
					co_await session.query("SELECT content_hash FROM config_version WHERE game_id=? AND version=?",
										   std::move(configKey));
				if (config.rows().empty())
				{
					throw ApiError(404, "配置版本不存在");
				}
				if (string(config.rows()[0][0]) != contentHash)
				{
					throw ApiError(409, "配置内容摘要不匹配");
				}
				std::vector<std::string> report {gameId,
												 std::to_string(serverId),
												 std::to_string(version),
												 contentHash};
				co_await session.query(
					"INSERT INTO config_server_state(game_id,server_id,version,content_hash,reported_at) "
					"VALUES(?,?,?,?,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE server_id=server_id",
					std::move(report));
				std::vector<std::string> stateKey {gameId, std::to_string(serverId)};
				auto current =
					co_await session.query("SELECT version FROM config_server_state WHERE game_id=? AND server_id=? "
										   "FOR UPDATE",
										   stateKey);
				if (integer(current.rows()[0][0]) > version)
				{
					throw ApiError(409, "旧应用报告不能覆盖新版本");
				}
				if (integer(current.rows()[0][0]) < version)
				{
					std::vector<std::string> update {std::to_string(version),
													 contentHash,
													 gameId,
													 std::to_string(serverId)};
					co_await session.query(
						"UPDATE config_server_state SET version=?,content_hash=?,reported_at=UTC_TIMESTAMP(6) "
						"WHERE game_id=? AND server_id=?",
						std::move(update));
				}
				co_await session.query("COMMIT");
				co_return boost::json::object {{"gameId", gameId},
											   {"serverId", serverId},
											   {"version", version},
											   {"contentHash", contentHash},
											   {"applied", true}};
			});
	}

	Awaitable<boost::json::array> Store::listConfigStates(std::string gameId, std::uint32_t afterServer, unsigned limit)
	{
		validateGame(gameId);
		validatePage(limit);
		auto session = co_await database_->acquire();
		std::vector<std::string> params {std::move(gameId), std::to_string(afterServer)};
		auto states =
			co_await session.query("SELECT server_id,version,content_hash,CAST(reported_at AS CHAR) "
								   "FROM config_server_state WHERE game_id=? AND server_id>? ORDER BY server_id LIMIT "
									   + std::to_string(limit),
								   std::move(params));
		boost::json::array items;
		for (auto row : states.rows())
		{
			items.push_back(boost::json::object {{"serverId", integer(row[0])},
												 {"version", integer(row[1])},
												 {"contentHash", string(row[2])},
												 {"reportedAt", string(row[3])}});
		}
		co_return items;
	}

	Awaitable<std::optional<DeliveryControl>> Store::readDeliveryControl(std::string gameId)
	{
		validateGame(gameId);
		std::vector<std::string> games {std::move(gameId)};
		auto controls = co_await readDeliveryControls(std::move(games));
		if (controls.empty())
		{
			co_return std::nullopt;
		}
		co_return std::move(controls.front());
	}

	Awaitable<std::vector<DeliveryControl>> Store::readDeliveryControls(std::vector<std::string> gameIds)
	{
		if (gameIds.empty())
		{
			co_return std::vector<DeliveryControl> {};
		}
		if (gameIds.size() > kMaxGames)
		{
			throw ApiError(400, "控制查询游戏范围过大");
		}
		std::string placeholders;
		for (const auto& gameId : gameIds)
		{
			validateGame(gameId);
			placeholders += placeholders.empty() ? "?" : ",?";
		}
		auto session = co_await database_->acquire();
		auto result =
			co_await session.query("SELECT game_id,version,enabled,max_concurrent_deliveries,max_concurrent_per_server "
								   "FROM game_delivery_control WHERE game_id IN ("
									   + placeholders + ") ORDER BY game_id",
								   std::move(gameIds));
		std::vector<DeliveryControl> controls;
		for (auto row : result.rows())
		{
			controls.push_back(controlFromRow(row));
		}
		co_return controls;
	}

	Awaitable<DeliveryControl> Store::updateDeliveryControl(DeliveryControl desired,
															std::uint64_t expectedVersion,
															std::string actor)
	{
		validateGame(desired.gameId);
		if (desired.maxConcurrentDeliveries > kMaxDeliveryConcurrency
			|| desired.maxConcurrentPerServer > kMaxDeliveryConcurrency || !validKey(actor)
			|| expectedVersion >= static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
		{
			throw ApiError(400, "投递控制参数无效");
		}
		desired.version = expectedVersion + 1;
		co_return co_await retryOperation<DeliveryControl>(
			"delivery_control",
			[this, &desired, expectedVersion, &actor]() -> Awaitable<DeliveryControl>
			{
				auto session = co_await database_->acquire();
				co_await session.query("START TRANSACTION");
				std::vector<std::string> params {std::to_string(desired.version),
												 desired.enabled ? "1" : "0",
												 std::to_string(desired.maxConcurrentDeliveries),
												 std::to_string(desired.maxConcurrentPerServer),
												 desired.gameId};
				if (expectedVersion == 0)
				{
					try
					{
						co_await session.query(
							"INSERT INTO game_delivery_control(version,enabled,max_concurrent_deliveries,"
							"max_concurrent_per_server,game_id,updated_at) VALUES(?,?,?,?,?,UTC_TIMESTAMP(6))",
							std::move(params));
					}
					catch (const boost::system::system_error& error)
					{
						if (error.code() != boost::mysql::common_server_errc::er_dup_entry)
						{
							throw;
						}
						throw ApiError(409, "投递控制版本冲突，请重新读取");
					}
				}
				else
				{
					params.push_back(std::to_string(expectedVersion));
					auto result = co_await session.query(
						"UPDATE game_delivery_control SET version=?,enabled=?,max_concurrent_deliveries=?,"
						"max_concurrent_per_server=?,updated_at=UTC_TIMESTAMP(6) WHERE game_id=? AND version=?",
						std::move(params));
					if (result.affected_rows() != 1)
					{
						throw ApiError(409, "投递控制版本冲突，请重新读取");
					}
				}
				co_await audit(session, actor, "delivery_control", desired.gameId, desired.version);
				co_await session.query("COMMIT");
				co_return desired;
			});
	}
} // namespace gamehub
