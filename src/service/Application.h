/** @file Application.h
 * @brief HTTP 路由、认证、后台任务和服务启停。
 */
#pragma once
#include "DeliveryWorker.h"
#include "core/GameAdapter.h"
#include <boost/asio/signal_set.hpp>
#include <core/HttpServer.h>

namespace gamehub
{
	class Application : public std::enable_shared_from_this<Application>
	{
	public:
		/// @param config 检查通过的完整配置。Application 实例必须由 shared_ptr 持有。
		explicit Application(Config config);
		/// @brief 注册路由和周期任务，然后阻塞运行服务；无返回值，启动失败时抛异常。
		void run();

	private:
		void registerRoutes();
		void registerTasks();
		Awaitable<hical::HttpResponse> handle(const hical::HttpRequest& request);
		Awaitable<hical::HttpResponse> dispatch(const hical::HttpRequest& request);
		Awaitable<hical::HttpResponse> callback(const hical::HttpRequest& request);
		Awaitable<hical::HttpResponse> adminWrite(const hical::HttpRequest& request, const std::string& actor);
		Awaitable<hical::HttpResponse> archive(const hical::HttpRequest& request, std::string actor);
		Awaitable<hical::HttpResponse> metrics();
		std::string authenticateAdmin(const hical::HttpRequest& request, bool write) const;
		Submission submission(const hical::HttpRequest& request, std::string taskType, std::string actor) const;
		void stop();

		// 按这个顺序声明，析构时会先释放应用组件，最后才释放 server 和 io_context。
		Config config_;
		hical::HttpServer server_;
		GameRegistry registry_;
		std::shared_ptr<Database> database_;
		Store store_;
		hical::HttpClientPool http_;
		DeliveryWorker worker_;
		boost::asio::signal_set signals_;
		bool schemaReady_ = false;
		bool stopping_ = false;
		std::uint64_t accepted_ = 0;
		std::uint64_t duplicates_ = 0;
		std::uint64_t rejected_ = 0;
	};
} // namespace gamehub
