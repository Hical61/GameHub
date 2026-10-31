/** @file response_limit_client.cpp
 * @brief 给原始 HTTP 测试服务发送请求，检查真实连接池的响应上限。
 */
#include <asio/HttpClientPool.h>
#include "core/Crypto.h"
#include <boost/asio/co_spawn.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/json.hpp>
#include <charconv>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace
{
	std::uint64_t number(std::string_view input)
	{
		std::uint64_t result = 0;
		const auto [end, error] = std::from_chars(input.data(), input.data() + input.size(), result);
		if (error != std::errc {} || end != input.data() + input.size())
		{
			throw std::invalid_argument("参数必须是非负整数");
		}
		return result;
	}

	hical::Awaitable<void> requests(hical::HttpClientPool& pool,
									std::uint16_t port,
									std::vector<std::size_t> limits,
									std::chrono::milliseconds requestTimeout)
	{
		const std::string host = "127.0.0.1";
		const hical::HeaderMap headers;
		for (std::size_t index = 0; index < limits.size(); ++index)
		{
			hical::HttpClientOptions options;
			options.maxResponseBodyBytes = limits[index];
			options.connectTimeout = std::chrono::seconds(2);
			options.requestTimeout = requestTimeout;
			const auto target = "/case/" + std::to_string(index);
			const auto started = std::chrono::steady_clock::now();
			const auto result = co_await pool.get(host, port, target, headers, options);
			const auto elapsed =
				std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started);
			std::cout << boost::json::serialize(
				boost::json::object {{"status", result.status},
									 {"error", result.isNetworkError()},
									 {"tooLarge", result.error.code == hical::ErrorCode::hResponseBodyTooLarge},
									 {"timedOut", result.error.code == hical::ErrorCode::hTimedOut},
									 {"bodyBytes", result.body.size()},
									 {"bodySha256", gamehub::sha256(result.body)},
									 {"interimHeader", !result.headers.find("X-Interim").empty()},
									 {"elapsedMs", elapsed.count()},
									 {"idle", pool.idleCount()}})
					  << '\n';
		}
	}
} // namespace

int main(int argc, char** argv)
{
	try
	{
		if (argc < 3)
		{
			throw std::invalid_argument(
				"用法：response_limit_client 端口 字节上限 [后续请求上限...] [--request-timeout-ms=毫秒]");
		}
		const auto port = number(argv[1]);
		if (port == 0 || port > std::numeric_limits<std::uint16_t>::max())
		{
			throw std::invalid_argument("端口超出范围");
		}
		std::vector<std::size_t> limits;
		auto requestTimeout = std::chrono::milliseconds(2000);
		for (int index = 2; index < argc; ++index)
		{
			const std::string_view argument = argv[index];
			constexpr std::string_view kTimeoutPrefix = "--request-timeout-ms=";
			if (argument.starts_with(kTimeoutPrefix))
			{
				const auto timeout = number(argument.substr(kTimeoutPrefix.size()));
				constexpr auto kMaxTestTimeoutMs = 10000;
				if (timeout == 0 || timeout > kMaxTestTimeoutMs)
				{
					throw std::invalid_argument("测试请求时限必须在 1 至 10000 毫秒之间");
				}
				requestTimeout = std::chrono::milliseconds(static_cast<std::int64_t>(timeout));
			}
			else
			{
				limits.push_back(static_cast<std::size_t>(number(argument)));
			}
		}
		if (limits.empty())
		{
			throw std::invalid_argument("至少需要一个响应体上限");
		}
		boost::asio::io_context io;
		hical::HttpClientPool pool(io);
		int exitCode = 0;
		boost::asio::co_spawn(io,
							  requests(pool, static_cast<std::uint16_t>(port), std::move(limits), requestTimeout),
							  [&pool, &exitCode](std::exception_ptr error)
							  {
								  exitCode = error ? 1 : 0;
								  pool.shutdown();
							  });
		io.run();
		return exitCode;
	}
	catch (const std::exception&)
	{
		std::cerr << "响应上限测试客户端执行失败\n";
		return 1;
	}
}
