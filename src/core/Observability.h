/** @file Observability.h
 * @brief 单事件循环使用的固定桶耗时统计，不保存请求或玩家数据。
 */
#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <string_view>

namespace gamehub
{
	class LatencyHistogram
	{
	public:
		/// @param seconds 非负耗时秒数。@return 无；固定内存，不分配样本数组。
		void observe(double seconds) noexcept;
		/// @param name 指标名；labels 已校验的配置标识构成的标签内容。@return Prometheus 累计桶、总和及数量。
		std::string prometheus(std::string_view name, std::string_view labels = {}) const;

	private:
		static constexpr std::array<double, 9> bounds_ {0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 30};
		std::array<std::uint64_t, bounds_.size()> buckets_ {};
		std::uint64_t count_ = 0;
		double sum_ = 0;
	};
} // namespace gamehub
