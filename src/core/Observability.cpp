#include "Observability.h"
#include <cmath>

namespace gamehub
{
	void LatencyHistogram::observe(double seconds) noexcept
	{
		if (!std::isfinite(seconds) || seconds < 0)
		{
			return;
		}
		++count_;
		sum_ += seconds;
		for (std::size_t index = 0; index < bounds_.size(); ++index)
		{
			buckets_[index] += seconds <= bounds_[index];
		}
	}

	std::string LatencyHistogram::prometheus(std::string_view name, std::string_view labels) const
	{
		std::string output;
		const auto bucketLabels = labels.empty() ? std::string {} : std::string(labels) + ",";
		for (std::size_t index = 0; index < bounds_.size(); ++index)
		{
			output += std::string(name) + "_bucket{" + bucketLabels + "le=\"" + std::to_string(bounds_[index]) + "\"} "
					  + std::to_string(buckets_[index]) + "\n";
		}
		output += std::string(name) + "_bucket{" + bucketLabels + "le=\"+Inf\"} " + std::to_string(count_) + "\n";
		const auto suffix = labels.empty() ? std::string {} : "{" + std::string(labels) + "}";
		output += std::string(name) + "_sum" + suffix + " " + std::to_string(sum_) + "\n";
		output += std::string(name) + "_count" + suffix + " " + std::to_string(count_) + "\n";
		return output;
	}
} // namespace gamehub
