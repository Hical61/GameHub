/** @file Database.h
 * @brief 带超时的数据库操作，连接归还后自动重置会话。
 */
#pragma once
#include "core/Config.h"
#include "core/Observability.h"
#include <boost/asio/awaitable.hpp>
#include <boost/mysql/connection_pool.hpp>
#include <boost/mysql/results.hpp>
#include <memory>
#include <string>
#include <vector>

namespace gamehub
{
	template <class T>
	using Awaitable = boost::asio::awaitable<T>;

	// 与连接池相同的事件循环内访问；无订单或SQL标签，内存占用固定。
	struct DatabaseMetrics
	{
		std::uint64_t queries = 0;
		std::uint64_t errors = 0;
		std::uint64_t deadlocks = 0;
		std::uint64_t cancellations = 0;
		std::uint64_t acquireErrors = 0;
		LatencyHistogram querySeconds;
		LatencyHistogram acquireSeconds;
	};

	class DbSession
	{
	public:
		/// @param connection 当前协程独占的连接；metrics 共享进程统计，可为空。析构归还连接，由池重置会话并回滚未提交事务。
		explicit DbSession(boost::mysql::pooled_connection connection, std::shared_ptr<DatabaseMetrics> metrics = {});
		/// @param sql 固定 SQL；params 待绑定的参数值。
		/// @return 独立持有数据的结果集；整次查询限时 5 秒，致命协议或取消错误会丢弃连接，不重放 SQL。
		/// 致命错误后释放当前会话，重新获取连接再发起新操作。
		Awaitable<boost::mysql::results> query(std::string sql, std::vector<std::string> params = {});

	private:
		boost::mysql::pooled_connection connection_;
		std::shared_ptr<DatabaseMetrics> metrics_;
	};

	class Database
	{
	public:
		/// @param executor 所属的事件循环；config 连接配置。所有调用都在这个事件循环中执行。
		Database(boost::asio::any_io_executor executor, const Config& config);
		/// @return 连接池的后台循环，只能启动一次。
		Awaitable<void> run();
		/// @return 当前协程独占的会话，最多等两秒获取连接。
		Awaitable<DbSession> acquire();
		/// @brief 取消后台循环和等待连接的操作；无返回值，在所属事件循环中调用。
		void stop();
		/// @return 本进程的数据库计数及固定桶耗时，只在所属事件循环读取。
		const DatabaseMetrics& metrics() const;

	private:
		boost::mysql::connection_pool pool_;
		std::shared_ptr<DatabaseMetrics> metrics_ = std::make_shared<DatabaseMetrics>();
	};
} // namespace gamehub
