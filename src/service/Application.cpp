/** @file Application.cpp
 * @brief 全部应用状态都在同一个事件循环中访问，连接池不跨线程使用。
 */
#include "Application.h"
#include "IngressLimits.h"
#include "core/Crypto.h"
#include <boost/asio/cancel_after.hpp>
#include <boost/asio/co_spawn.hpp>
#include <boost/asio/detached.hpp>
#include <boost/asio/use_awaitable.hpp>
#include <boost/system/system_error.hpp>
#include <charconv>
#include <core/JwtAuth.h>
#include <core/Log.h>
#include <algorithm>
#include <csignal>
#include <limits>

namespace gamehub
{
	namespace
	{
		constexpr auto kCallbackTimeout = std::chrono::milliseconds(2500);
		constexpr auto kMonitorInterval = std::chrono::seconds(30);
		constexpr double kIdleTimeoutSeconds = 15;
		constexpr double kShutdownTimeoutSeconds = 20;
		constexpr unsigned kBacklogAlertSeconds = 60;

		hical::HttpResponse response(int status, const boost::json::value& body)
		{
			auto result = hical::HttpResponse::json(body);
			result.setStatus(static_cast<hical::HttpStatusCode>(status));
			result.setHeader("Cache-Control", "no-store");
			return result;
		}

		std::uint64_t pathNumber(std::string_view value, bool allowZero = false)
		{
			if (value.empty())
			{
				throw ApiError(400, "数值参数无效");
			}
			std::uint64_t result = 0;
			const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
			constexpr auto kMaxJsonInteger = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
			if (error != std::errc {} || end != value.data() + value.size() || (!allowZero && result == 0)
				|| result > kMaxJsonInteger)
			{
				throw ApiError(400, "数值参数无效");
			}
			return result;
		}

		boost::json::object acceptedBody(const AcceptResult& result)
		{
			return {{"eventId", std::to_string(result.eventId)}, {"duplicate", result.duplicate}, {"accepted", true}};
		}

		SignedRequest signedRequest(const hical::HttpRequest& request)
		{
			return {request.header("X-Timestamp"),
					request.header("X-Signature"),
					request.method() == hical::HttpMethod::hPost ? "POST" : "GET",
					request.path(),
					request.body()};
		}

		hical::HttpResponse callbackResponse(CallbackReply reply)
		{
			hical::HttpResponse result;
			result.setStatus(static_cast<hical::HttpStatusCode>(reply.status));
			result.setHeader("Cache-Control", "no-store");
			result.setBody(std::move(reply.body), reply.contentType);
			return result;
		}
	} // namespace

	Application::Application(Config config, GameRegistry registry)
		: config_(std::move(config))
		, server_(config_.port, 1)
		, registry_(std::move(registry))
		, database_(std::make_shared<Database>(server_.ioContext().get_executor(), config_))
		, store_(database_)
		, http_(server_.ioContext())
		, worker_(config_, registry_, store_, http_)
		, signals_(server_.ioContext(), SIGINT, SIGTERM)
	{
		for (const auto& game : config_.games)
		{
			if (config_.worker.games.empty()
				|| std::find(config_.worker.games.begin(), config_.worker.games.end(), game.id)
					   != config_.worker.games.end())
			{
				recoveryGames_.push_back(game.id);
			}
		}
		server_.listen(config_.listen, config_.port);
		server_.setMaxBodySize(config_.maxBodyBytes);
		server_.setMaxConnections(config_.maxConnections);
		server_.setIdleTimeout(kIdleTimeoutSeconds);
		server_.setShutdownTimeout(kShutdownTimeoutSeconds);
	}

	void Application::run()
	{
		registerRoutes();
		registerTasks();
		// 后台操作只保留数据库引用，免得 Application 和回调互相持有，无法释放。
		boost::asio::co_spawn(server_.ioContext(),
							  database_->run(),
							  [database = database_](std::exception_ptr error)
							  {
								  if (error)
								  {
									  HICAL_LOG_ERROR("数据库连接池后台循环异常退出");
								  }
							  });
		signals_.async_wait(
			[weak = weak_from_this()](const boost::system::error_code& error, int)
			{
				if (auto self = weak.lock(); self && !error)
				{
					self->stop();
				}
			});
		HICAL_LOG_INFO("GameHub 0.1 启动，监听 {}:{}，已注册 {} 款游戏",
					   config_.listen,
					   config_.port,
					   config_.games.size());
		try
		{
			server_.start();
		}
		catch (...)
		{
			stop();
			throw;
		}
	}

	void Application::stop()
	{
		if (stopping_)
		{
			return;
		}
		stopping_ = true;
		schemaReady_ = false;
		database_->stop();
		http_.shutdown();
		signals_.cancel();
		server_.stop();
	}

	void Application::registerRoutes()
	{
		server_.use(makeIngressLimits(config_));
		const auto handler =
			[weak = weak_from_this()](const hical::HttpRequest& request) -> hical::Awaitable<hical::HttpResponse>
		{
			auto self = weak.lock();
			if (!self)
			{
				co_return response(503, boost::json::object {{"error", "服务已停止"}});
			}
			co_return co_await self->handle(request);
		};
		auto& router = server_.router();
		router.get("/admin/health", handler);
		router.get("/admin/ready", handler);
		router.get("/admin/stats", handler);
		router.get("/admin/stats/{game}", handler);
		router.get("/admin/runtime", handler);
		router.get("/admin/runtime/{game}", handler);
		router.get("/admin/metrics", handler);
		router.get("/admin/tasks/{game}/{after}", handler);
		router.get("/admin/archive/{game}/{after}", handler);
		router.get("/admin/audit/{game}/{after}", handler);
		router.post("/admin/tasks/{game}/{id}/replay", handler);
		router.post("/admin/archive/{game}", handler);
		router.post("/admin/announce/{game}", handler);
		router.post("/admin/config/{game}/{version}", handler);
		router.post("/api/callback/{game}/{channel}", handler);
		router.get("/internal/config/{game}/{server}/{version}", handler);
	}

	void Application::registerTasks()
	{
		const auto weak = weak_from_this();
		const auto interval = std::chrono::milliseconds(config_.worker.pollMs);
		server_.addPeriodicTask("schema-and-recovery",
								interval,
								[weak]() -> Awaitable<void>
								{
									auto self = weak.lock();
									if (!self || self->stopping_)
									{
										co_return;
									}
									try
									{
										co_await self->store_.checkSchema();
										self->schemaReady_ = true;
										const auto count =
											co_await self->store_.recover(self->config_.worker, self->recoveryGames_);
										if (count > 0)
										{
											HICAL_LOG_WARN("已回收过期的投递租约，count={}", count);
										}
									}
									catch (const boost::system::system_error& error)
									{
										self->schemaReady_ = false;
										HICAL_LOG_ERROR("数据库或迁移检查失败，category={} code={}，暂不接收业务请求",
														error.code().category().name(),
														error.code().value());
									}
									catch (const std::exception&)
									{
										self->schemaReady_ = false;
										HICAL_LOG_ERROR("数据库或迁移检查失败，暂不接收业务请求");
									}
								});
		for (unsigned slot = 0; slot < config_.worker.concurrency; ++slot)
		{
			server_.addPeriodicTask("delivery-" + std::to_string(slot),
									interval,
									[weak]() -> Awaitable<void>
									{
										auto self = weak.lock();
										if (self && self->schemaReady_ && !self->stopping_)
										{
											try
											{
												co_await self->worker_.tick();
											}
											catch (const boost::system::system_error& error)
											{
												HICAL_LOG_ERROR(
													"投递时数据库操作失败，category={} code={}，任务将等待租约回收",
													error.code().category().name(),
													error.code().value());
											}
											catch (const std::exception&)
											{
												HICAL_LOG_ERROR("投递时数据库操作失败，任务将等待租约回收");
											}
										}
									});
		}
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

	Awaitable<hical::HttpResponse> Application::handle(const hical::HttpRequest& request)
	{
		try
		{
			if (!request.path().starts_with("/api/callback/") && request.target().find('?') != std::string_view::npos)
			{
				throw ApiError(400, "当前接口不接受查询参数");
			}
			co_return co_await dispatch(request);
		}
		catch (const ApiError& error)
		{
			++rejected_;
			co_return response(error.status, boost::json::object {{"error", error.what()}});
		}
		catch (const boost::system::system_error& error)
		{
			++rejected_;
			HICAL_LOG_ERROR("处理请求时依赖服务出错，category={} code={}",
							error.code().category().name(),
							error.code().value());
			co_return response(503, boost::json::object {{"error", "依赖服务暂时不可用，请保留原业务键重试"}});
		}
		catch (const std::exception&)
		{
			++rejected_;
			HICAL_LOG_ERROR("请求处理失败");
			co_return response(503, boost::json::object {{"error", "依赖服务暂时不可用，请保留原业务键重试"}});
		}
	}

	std::string Application::authenticateAdmin(const hical::HttpRequest& request, bool write) const
	{
		const auto authorization = request.header("Authorization");
		constexpr std::string_view kPrefix = "Bearer ";
		if (!authorization.starts_with(kPrefix) || authorization.size() > 4096)
		{
			throw ApiError(401, "请提供有效的管理令牌");
		}
		boost::json::object claims;
		try
		{
			hical::JwtAuthOptions options;
			options.secret = config_.jwtSecret;
			options.issuer = config_.jwtIssuer;
			options.audience = config_.jwtAudience;
			claims = hical::jwtVerify(authorization.substr(kPrefix.size()), options);
			auto* expiry = claims.if_contains("exp");
			if (!expiry || !expiry->is_int64() || expiry->as_int64() <= unixSeconds())
			{
				throw ApiError(401, "管理令牌缺少过期时间，或已经过期");
			}
		}
		catch (const std::exception&)
		{
			throw ApiError(401, "管理令牌无效");
		}
		std::string actor;
		std::string role;
		try
		{
			actor = requiredString(claims, "sub", 96);
			role = requiredString(claims, "role", 16);
		}
		catch (const ApiError&)
		{
			throw ApiError(401, "管理令牌的主体或角色缺失、格式不对");
		}
		if (!validKey(actor))
		{
			throw ApiError(401, "管理令牌主体无效");
		}
		if ((role != "viewer" && role != "operator") || (write && role != "operator"))
		{
			throw ApiError(403, "管理权限不足");
		}
		const auto& gameId = request.param("game");
		auto* allowedGames = claims.if_contains("games");
		if (!allowedGames || !allowedGames->is_array())
		{
			throw ApiError(403, "管理令牌没有指定可访问的游戏");
		}
		bool allowed = gameId.empty();
		for (const auto& game : allowedGames->as_array())
		{
			if (game.is_string() && (game.as_string() == gameId || game.as_string() == "*"))
			{
				allowed = true;
			}
		}
		// 全局统计会包含其他游戏的数据，必须拿到全局授权才能读取。
		if (gameId.empty())
		{
			allowed = false;
			for (const auto& game : allowedGames->as_array())
			{
				if (game.is_string() && game.as_string() == "*")
				{
					allowed = true;
				}
			}
		}
		if (!allowed)
		{
			throw ApiError(403, "没有此游戏的管理权限");
		}
		return actor;
	}

	Awaitable<hical::HttpResponse> Application::callback(const hical::HttpRequest& request)
	{
		const auto& channel = registry_.channel(request.param("game"), request.param("channel"));
		CallbackResult outcome;
		try
		{
			if (!schemaReady_ || stopping_)
			{
				throw ApiError(503, "服务未就绪");
			}
			const CallbackRequest view {.method = hical::httpMethodToString(request.method()),
										.path = request.path(),
										.target = request.target(),
										.body = request.body(),
										.header = [&request](std::string_view name)
										{
											return request.header(name);
										}};
			auto input = channel.parseCallback(view, unixSeconds());
			const auto result =
				co_await boost::asio::co_spawn(server_.ioContext(),
											   store_.accept(std::move(input)),
											   boost::asio::cancel_after(kCallbackTimeout, boost::asio::use_awaitable));
			++accepted_;
			duplicates_ += result.duplicate;
			outcome = {.status = 200, .eventId = result.eventId, .duplicate = result.duplicate};
		}
		catch (const ApiError& error)
		{
			++rejected_;
			outcome.status = error.status;
			outcome.message = error.what();
		}
		catch (const std::exception&)
		{
			++rejected_;
			HICAL_LOG_ERROR("渠道请求受理失败，请按原业务身份重试");
			outcome.status = 503;
			outcome.message = "依赖服务暂时不可用，请保留原业务键重试";
		}
		co_return callbackResponse(channel.reply(outcome));
	}

	Submission Application::submission(const hical::HttpRequest& request, std::string taskType, std::string actor) const
	{
		const auto& game = config_.game(request.param("game"));
		auto body = parseObject(request.body());
		Submission input;
		input.gameId = game.id;
		input.taskType = std::move(taskType);
		input.nameSpace = "admin";
		input.actor = std::move(actor);
		input.bizKey = requiredString(body, "bizKey", kMaxKeyLength);
		if (!validKey(input.bizKey))
		{
			throw ApiError(400, "业务键无效");
		}
		auto serverId = static_cast<std::uint32_t>(requiredInt(body, "serverId", 0, UINT32_MAX));
		if (serverId == 0)
		{
			for (const auto& endpoint : game.servers)
			{
				input.servers.push_back(endpoint.id);
			}
		}
		else
		{
			game.endpoint(serverId);
			input.servers = {serverId};
		}
		input.payload = canonicalJson(body);
		if (input.payload.size() > kMaxPayloadBytes)
		{
			throw ApiError(413, "业务内容超过大小限制");
		}
		input.fingerprint = sha256(input.payload);
		return input;
	}

	Awaitable<hical::HttpResponse> Application::adminWrite(const hical::HttpRequest& request, const std::string& actor)
	{
		const auto gameId = request.param("game");
		config_.game(gameId);
		if (request.path().starts_with("/admin/archive/"))
		{
			co_return co_await archive(request, actor);
		}
		if (request.path().starts_with("/admin/tasks/"))
		{
			if (!request.body().empty())
			{
				throw ApiError(400, "重排接口请求体必须为空");
			}
			const bool replayed = co_await store_.replay(gameId, pathNumber(request.param("id")), actor);
			if (!replayed)
			{
				throw ApiError(409, "任务不存在，或当前不是 failed 状态");
			}
			co_return response(200, boost::json::object {{"replayed", true}});
		}
		if (request.path().starts_with("/admin/announce/"))
		{
			auto input = submission(request, "announce", actor);
			const auto body = parseObject(input.payload);
			if (body.size() != 3)
			{
				throw ApiError(400, "公告只能包含 bizKey、serverId、content");
			}
			requiredString(body, "content", 8192);
			const auto result = co_await store_.accept(std::move(input));
			co_return response(200, acceptedBody(result));
		}
		const auto version = pathNumber(request.param("version"));
		auto input = submission(request, "config_push", actor);
		const auto body = parseObject(input.payload);
		if (body.size() != 3 || body.at("serverId").to_number<std::uint64_t>() != 0 || !body.if_contains("content"))
		{
			throw ApiError(400, "配置发布需要 bizKey、serverId=0 和 content");
		}
		if (input.bizKey != "config:" + std::to_string(version))
		{
			throw ApiError(400, "配置业务键必须为 config:版本号");
		}
		const auto content = canonicalJson(body.at("content"));
		input.payload = canonicalJson(boost::json::object {{"version", version}});
		input.fingerprint = sha256(content);
		const auto result = co_await store_.publishConfig(std::move(input), version, content);
		co_return response(200, acceptedBody(result));
	}

	Awaitable<hical::HttpResponse> Application::dispatch(const hical::HttpRequest& request)
	{
		const auto path = request.path();
		if (path == "/admin/health")
		{
			co_return response(200, boost::json::object {{"status", "ok"}});
		}
		if (path == "/admin/ready")
		{
			co_return response(schemaReady_ && !stopping_ ? 200 : 503,
							   boost::json::object {{"ready", schemaReady_ && !stopping_}});
		}
		if (path.starts_with("/api/callback/"))
		{
			co_return co_await callback(request);
		}
		if (!schemaReady_ || stopping_)
		{
			throw ApiError(503, "服务未就绪");
		}
		if (path.starts_with("/internal/config/"))
		{
			const auto& game = config_.game(request.param("game"));
			const auto server = pathNumber(request.param("server"));
			if (server > UINT32_MAX)
			{
				throw ApiError(400, "区服编号越界");
			}
			game.endpoint(static_cast<std::uint32_t>(server));
			if (!registry_.find(game.id).verifyInternalRequest(signedRequest(request), unixSeconds()))
			{
				throw ApiError(401, "签名无效或请求已过期");
			}
			co_return response(200, co_await store_.readConfig(game.id, pathNumber(request.param("version"), true)));
		}
		const bool write = request.method() == hical::HttpMethod::hPost;
		const auto actor = authenticateAdmin(request, write);
		if (write)
		{
			co_return co_await adminWrite(request, actor);
		}
		co_return co_await adminRead(request);
	}

	Awaitable<hical::HttpResponse> Application::adminRead(const hical::HttpRequest& request)
	{
		const auto path = request.path();
		if (path == "/admin/metrics")
		{
			co_return co_await metrics();
		}
		if (path == "/admin/runtime" || path.starts_with("/admin/runtime/"))
		{
			const auto gameId = request.param("game");
			if (!gameId.empty())
			{
				config_.game(gameId);
			}
			co_return response(200, worker_.snapshot(gameId));
		}
		if (path.starts_with("/admin/stats/"))
		{
			const auto& game = config_.game(request.param("game"));
			co_return response(200, co_await store_.stats(game.id));
		}
		if (path == "/admin/stats")
		{
			auto stats = co_await store_.stats();
			stats["callbacksAccepted"] = accepted_;
			stats["callbacksDuplicate"] = duplicates_;
			stats["requestsRejected"] = rejected_;
			co_return response(200, stats);
		}
		const auto& game = config_.game(request.param("game"));
		auto after = pathNumber(request.param("after"), true);
		if (path.starts_with("/admin/archive/"))
		{
			co_return response(200, boost::json::object {{"items", co_await store_.listArchived(game.id, after)}});
		}
		auto items = path.starts_with("/admin/tasks/") ? co_await store_.listTasks(game.id, after)
													   : co_await store_.listAudit(game.id, after);
		co_return response(200, boost::json::object {{"items", std::move(items)}});
	}

	Awaitable<hical::HttpResponse> Application::archive(const hical::HttpRequest& request, std::string actor)
	{
		const auto body = parseObject(request.body());
		const auto* redact = body.if_contains("redactPayload");
		if (body.size() != 3 || !redact || !redact->is_bool())
		{
			throw ApiError(400, "归档需要 keepDays、batchSize 和布尔值 redactPayload");
		}
		const auto keepDays =
			static_cast<unsigned>(requiredInt(body, "keepDays", kMinRetentionDays, kMaxRetentionDays));
		const auto batchSize = static_cast<unsigned>(requiredInt(body, "batchSize", 1, kMaxMaintenanceBatch));
		const auto gameId = request.param("game");
		const auto archived = co_await store_.archiveDone(gameId, keepDays, batchSize, actor);
		const auto redacted =
			redact->as_bool() ? co_await store_.redactCompleted(gameId, keepDays, batchSize, actor) : 0;
		co_return response(200, boost::json::object {{"archivedTasks", archived}, {"redactedEvents", redacted}});
	}

	Awaitable<hical::HttpResponse> Application::metrics()
	{
		const auto stats = co_await store_.stats();
		std::string body = "# HELP gamehub_tasks 按状态统计的当前任务数。\n# TYPE gamehub_tasks gauge\n";
		for (const auto* state : {"pending", "delivering", "done", "failed"})
		{
			body += "gamehub_tasks{state=\"" + std::string(state) + "\"} "
					+ std::to_string(stats.at(state).to_number<std::uint64_t>()) + "\n";
		}
		const auto append = [&body](std::string_view name, std::string_view type, std::uint64_t value)
		{
			body += "# TYPE " + std::string(name) + " " + std::string(type) + "\n" + std::string(name) + " "
					+ std::to_string(value) + "\n";
		};
		append("gamehub_archived_tasks", "gauge", stats.at("archivedDone").to_number<std::uint64_t>());
		append("gamehub_oldest_pending_seconds", "gauge", stats.at("oldestPendingSeconds").to_number<std::uint64_t>());
		append("gamehub_callbacks_accepted_total", "counter", accepted_);
		append("gamehub_callbacks_duplicate_total", "counter", duplicates_);
		append("gamehub_requests_rejected_total", "counter", rejected_);
		hical::HttpResponse result;
		result.setStatus(static_cast<hical::HttpStatusCode>(200));
		result.setHeader("Cache-Control", "no-store");
		result.setBody(std::move(body), "text/plain; version=0.0.4; charset=utf-8");
		co_return result;
	}
} // namespace gamehub
