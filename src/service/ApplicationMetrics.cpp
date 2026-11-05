/** @file ApplicationMetrics.cpp
 * @brief 数据库持久统计与进程指标使用不同名字，避免多实例误加总。
 */
#include "Application.h"

namespace gamehub
{
	namespace
	{
		void append(std::string& body, std::string_view name, std::uint64_t value, std::string_view labels = {})
		{
			body += std::string(name) + (labels.empty() ? "" : "{" + std::string(labels) + "}") + " "
					+ std::to_string(value) + "\n";
		}

		void appendResults(std::string& body,
						   std::string_view prefix,
						   const boost::json::object& counters,
						   const std::string& labels)
		{
			append(body,
				   std::string(prefix) + "_claimed_total",
				   counters.at("claimed").to_number<std::uint64_t>(),
				   labels);
			append(body,
				   std::string(prefix) + "_failures_total",
				   counters.at("failed").to_number<std::uint64_t>(),
				   labels);
			constexpr std::array<std::pair<std::string_view, std::string_view>, 10> outcomes {
				{{"succeeded", "success"},
				 {"networkErrors", "network_error"},
				 {"timeouts", "timeout"},
				 {"invalidAcks", "invalid_ack"},
				 {"responseTooLarge", "response_too_large"},
				 {"httpErrors", "http_error"},
				 {"exceptions", "exception"},
				 {"storageErrors", "storage_error"},
				 {"leaseLost", "lease_lost"},
				 {"interrupted", "interrupted"}}};
			for (const auto& [field, outcome] : outcomes)
			{
				auto count = counters.at(field).to_number<std::uint64_t>();
				// networkErrors包含timeouts，导出互斥结果时扣除，避免汇总重复计数。
				if (field == "networkErrors")
				{
					count -= counters.at("timeouts").to_number<std::uint64_t>();
				}
				append(body,
					   std::string(prefix) + "_results_total",
					   count,
					   labels + ",result=\"" + std::string(outcome) + "\"");
			}
		}

		void runtimeMetrics(std::string& body, const boost::json::object& runtime)
		{
			for (const auto& item : runtime.at("games").as_array())
			{
				const auto& game = item.as_object();
				const auto label = "game=\"" + std::string(game.at("gameId").as_string()) + "\"";
				append(body, "gamehub_delivery_enabled", game.at("enabled").as_bool(), label);
				append(body, "gamehub_delivery_in_flight", game.at("inFlight").to_number<std::uint64_t>(), label);
				appendResults(body, "gamehub_delivery", game, label);
				for (const auto& value : game.at("targets").as_array())
				{
					const auto& target = value.as_object();
					if (target.at("claimed").to_number<std::uint64_t>() == 0)
					{
						continue;
					}
					const auto targetLabel =
						label + ",server=\"" + std::to_string(target.at("serverId").to_number<std::uint64_t>()) + "\"";
					append(body,
						   "gamehub_target_in_flight",
						   target.at("inFlight").to_number<std::uint64_t>(),
						   targetLabel);
					appendResults(body, "gamehub_target", target, targetLabel);
				}
			}
		}

		void appendTypes(std::string& body)
		{
			for (const auto* name : {"gamehub_game_tasks",
									 "gamehub_game_oldest_pending_seconds",
									 "gamehub_archived_tasks",
									 "gamehub_oldest_pending_seconds",
									 "gamehub_delivery_enabled",
									 "gamehub_delivery_in_flight",
									 "gamehub_target_in_flight"})
			{
				body += "# TYPE " + std::string(name) + " gauge\n";
			}
			for (const auto* name : {"gamehub_callback_seconds",
									 "gamehub_delivery_seconds",
									 "gamehub_completion_seconds",
									 "gamehub_db_query_seconds",
									 "gamehub_db_acquire_seconds"})
			{
				body += "# TYPE " + std::string(name) + " histogram\n";
			}
			for (const auto* name : {"gamehub_callbacks_accepted_total",
									 "gamehub_callbacks_duplicate_total",
									 "gamehub_requests_rejected_total",
									 "gamehub_leases_recovered_total",
									 "gamehub_db_queries_total",
									 "gamehub_db_errors_total",
									 "gamehub_db_deadlocks_total",
									 "gamehub_db_cancellations_total",
									 "gamehub_db_acquire_errors_total",
									 "gamehub_delivery_results_total",
									 "gamehub_target_results_total",
									 "gamehub_delivery_claimed_total",
									 "gamehub_delivery_failures_total",
									 "gamehub_target_claimed_total",
									 "gamehub_target_failures_total"})
			{
				body += "# TYPE " + std::string(name) + " counter\n";
			}
		}

		void appendDatabaseMetrics(std::string& body, const DatabaseMetrics& database)
		{
			append(body, "gamehub_db_queries_total", database.queries);
			append(body, "gamehub_db_errors_total", database.errors);
			append(body, "gamehub_db_deadlocks_total", database.deadlocks);
			append(body, "gamehub_db_cancellations_total", database.cancellations);
			append(body, "gamehub_db_acquire_errors_total", database.acquireErrors);
			body += database.querySeconds.prometheus("gamehub_db_query_seconds");
			body += database.acquireSeconds.prometheus("gamehub_db_acquire_seconds");
		}
	} // namespace

	Awaitable<hical::HttpResponse> Application::metrics()
	{
		const auto stats = co_await store_.stats();
		std::string body =
			"# HELP gamehub_tasks 共享数据库任务数，多实例抓取后应取max而非sum。\n# TYPE gamehub_tasks gauge\n";
		appendTypes(body);
		std::vector<std::string> gameIds;
		for (const auto& game : config_.games)
		{
			gameIds.push_back(game.id);
		}
		for (const auto& value : co_await store_.gameStats(std::move(gameIds)))
		{
			const auto& game = value.as_object();
			const auto label = "game=\"" + std::string(game.at("gameId").as_string()) + "\"";
			for (const auto* state : {"pending", "delivering", "done", "failed"})
			{
				append(body,
					   "gamehub_game_tasks",
					   game.at(state).to_number<std::uint64_t>(),
					   label + ",state=\"" + state + "\"");
			}
			append(body,
				   "gamehub_game_oldest_pending_seconds",
				   game.at("oldestPendingSeconds").to_number<std::uint64_t>(),
				   label);
		}
		for (const auto* state : {"pending", "delivering", "done", "failed"})
		{
			append(body,
				   "gamehub_tasks",
				   stats.at(state).to_number<std::uint64_t>(),
				   "state=\"" + std::string(state) + "\"");
		}
		append(body, "gamehub_archived_tasks", stats.at("archivedDone").to_number<std::uint64_t>());
		append(body, "gamehub_oldest_pending_seconds", stats.at("oldestPendingSeconds").to_number<std::uint64_t>());
		append(body, "gamehub_callbacks_accepted_total", accepted_);
		append(body, "gamehub_callbacks_duplicate_total", duplicates_);
		append(body, "gamehub_requests_rejected_total", rejected_);
		append(body, "gamehub_leases_recovered_total", recovered_);
		appendDatabaseMetrics(body, database_->metrics());
		for (const auto& [game, latency] : callbackLatencies_)
		{
			body += latency.prometheus("gamehub_callback_seconds", "game=\"" + game + "\"");
		}
		runtimeMetrics(body, worker_.snapshot());
		body += worker_.latencyMetrics();
		hical::HttpResponse result;
		result.setHeader("Cache-Control", "no-store");
		result.setBody(std::move(body), "text/plain; version=0.0.4; charset=utf-8");
		co_return result;
	}
} // namespace gamehub
