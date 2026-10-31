/** @file Database.cpp
 * @brief 使用 Boost.MySQL 连接池，连接归还后由池重置会话并回滚未提交事务。
 */
#include "Database.h"
#include <boost/asio/cancel_after.hpp>
#include <boost/asio/cancel_at.hpp>
#include <boost/asio/use_awaitable.hpp>
#include <boost/mysql.hpp>
#include <chrono>

namespace gamehub
{
	namespace
	{
		constexpr auto kAcquireTimeout = std::chrono::seconds(2);
		constexpr auto kQueryTimeout = std::chrono::seconds(5);

		boost::mysql::pool_params poolParams(const Config& config)
		{
			boost::mysql::pool_params params;
			params.server_address.emplace_host_and_port(config.dbHost, config.dbPort);
			params.username = config.dbUser;
			params.password = config.dbPassword;
			params.database = config.dbName;
			params.initial_size = 1;
			params.max_size = config.dbMaxConnections;
			params.connect_timeout = kAcquireTimeout;
			params.retry_interval = std::chrono::seconds(1);
			params.ping_interval = std::chrono::seconds(30);
			return params;
		}
	} // namespace

	DbSession::DbSession(boost::mysql::pooled_connection connection) : connection_(std::move(connection))
	{
	}

	Awaitable<boost::mysql::results> DbSession::query(std::string sql, std::vector<std::string> params)
	{
		namespace asio = boost::asio;
		const auto deadline = std::chrono::steady_clock::now() + kQueryTimeout;
		boost::mysql::results result;
		try
		{
			if (params.empty())
			{
				co_await connection_->async_execute(sql, result, asio::cancel_at(deadline, asio::use_awaitable));
			}
			else
			{
				auto statement =
					co_await connection_->async_prepare_statement(sql, asio::cancel_at(deadline, asio::use_awaitable));
				std::vector<boost::mysql::field_view> fields;
				fields.reserve(params.size());
				for (const auto& param : params)
				{
					fields.emplace_back(param);
				}
				co_await connection_->async_execute(statement.bind(fields.begin(), fields.end()),
													result,
													asio::cancel_at(deadline, asio::use_awaitable));
				co_await connection_->async_close_statement(statement, asio::cancel_at(deadline, asio::use_awaitable));
			}
		}
		catch (const boost::system::system_error& error)
		{
			if (boost::mysql::is_fatal_error(error.code()))
			{
				// 取消后可能残留旧响应，不能让连接池误当成 reset 成功后继续复用。
				// 当前池使用默认连接参数；替换会同步关闭旧连接，归池后由池重新连接。
				connection_.get() = boost::mysql::any_connection(connection_->get_executor());
			}
			throw;
		}
		co_return result;
	}

	Database::Database(boost::asio::any_io_executor executor, const Config& config)
		: pool_(std::move(executor), poolParams(config))
	{
	}

	Awaitable<void> Database::run()
	{
		co_await pool_.async_run(boost::asio::use_awaitable);
	}

	Awaitable<DbSession> Database::acquire()
	{
		auto connection =
			co_await pool_.async_get_connection(boost::asio::cancel_after(kAcquireTimeout, boost::asio::use_awaitable));
		co_return DbSession(std::move(connection));
	}

	void Database::stop()
	{
		pool_.cancel();
	}
} // namespace gamehub
