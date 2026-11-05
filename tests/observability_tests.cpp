/** @file observability_tests.cpp
 * @brief 验证累计直方图的边界、非有限输入和输出序列。
 */
#include "core/Observability.h"
#include <iostream>
#include <limits>
#include <stdexcept>

namespace
{
	void require(bool condition, const char* message)
	{
		if (!condition)
		{
			throw std::runtime_error(message);
		}
	}

	void verifyHistogram()
	{
		gamehub::LatencyHistogram histogram;
		for (double value : {0.0, 0.001, 0.005, 0.02, 40.0})
		{
			histogram.observe(value);
		}
		histogram.observe(-1);
		histogram.observe(std::numeric_limits<double>::quiet_NaN());
		histogram.observe(std::numeric_limits<double>::infinity());
		const auto text = histogram.prometheus("latency", "game=\"demo\"");
		require(text.find("latency_bucket{game=\"demo\",le=\"0.001000\"} 2\n") != std::string::npos,
				"第一桶没有包含零和边界值");
		require(text.find("latency_bucket{game=\"demo\",le=\"0.005000\"} 3\n") != std::string::npos,
				"累计桶未包含更小值");
		require(text.find("latency_bucket{game=\"demo\",le=\"30.000000\"} 4\n") != std::string::npos,
				"有限最大桶错误包含超限值");
		require(text.find("latency_bucket{game=\"demo\",le=\"+Inf\"} 5\n") != std::string::npos
					&& text.find("latency_count{game=\"demo\"} 5\n") != std::string::npos,
				"无穷桶或总数包含非法输入");
		require(text.find("latency_sum{game=\"demo\"} 40.026000\n") != std::string::npos, "耗时总和不正确");
		gamehub::LatencyHistogram empty;
		const auto zero = empty.prometheus("empty");
		require(zero.find("empty_bucket{le=\"+Inf\"} 0\n") != std::string::npos
					&& zero.find("empty_count 0\n") != std::string::npos,
				"空直方图输出无标签格式不合法");
	}
} // namespace

int main()
{
	try
	{
		verifyHistogram();
		std::cout << "耗时直方图测试通过\n";
		return 0;
	}
	catch (const std::exception& error)
	{
		std::cerr << "耗时直方图测试失败：" << error.what() << '\n';
		return 1;
	}
}
