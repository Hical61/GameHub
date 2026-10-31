/** @file Domain.cpp
 * @brief 检查业务字段，按固定顺序序列化 JSON。
 */
#include "Domain.h"
#include <algorithm>
#include <chrono>
#include <limits>

namespace gamehub
{
	bool validKey(std::string_view text, std::size_t maxLength)
	{
		return !text.empty() && text.size() <= maxLength
			   && std::all_of(text.begin(),
							  text.end(),
							  [](unsigned char c)
							  {
								  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
										 || c == '_' || c == '-' || c == ':' || c == '.';
							  });
	}

	std::string requiredString(const boost::json::object& object, std::string_view key, std::size_t maxLength)
	{
		auto* value = object.if_contains(key);
		if (!value || !value->is_string() || value->as_string().empty() || value->as_string().size() > maxLength)
		{
			throw ApiError(400, "字符串字段缺失或格式不对：" + std::string(key));
		}
		std::string result(value->as_string());
		if (result.find('\0') != std::string::npos)
		{
			throw ApiError(400, "字符串不能包含空字符");
		}
		return result;
	}

	std::int64_t requiredInt(const boost::json::object& object,
							 std::string_view key,
							 std::int64_t min,
							 std::int64_t max)
	{
		auto* value = object.if_contains(key);
		if (!value || (!value->is_int64() && !value->is_uint64()))
		{
			throw ApiError(400, "字段缺失或不是整数：" + std::string(key));
		}
		if (value->is_uint64()
			&& value->as_uint64() > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
		{
			throw ApiError(400, "整数超出范围");
		}
		auto number = value->is_int64() ? value->as_int64() : static_cast<std::int64_t>(value->as_uint64());
		if (number < min || number > max)
		{
			throw ApiError(400, "整数超出范围：" + std::string(key));
		}
		return number;
	}

	boost::json::object parseObject(std::string_view body)
	{
		boost::system::error_code error;
		auto value = boost::json::parse(body, error);
		if (error || !value.is_object())
		{
			throw ApiError(400, "请求体必须是 JSON 对象");
		}
		return std::move(value.as_object());
	}

	namespace
	{
		boost::json::value sortedValue(const boost::json::value& value)
		{
			if (value.is_object())
			{
				std::vector<std::string> keys;
				for (const auto& item : value.as_object())
				{
					keys.emplace_back(item.key());
				}
				std::sort(keys.begin(), keys.end());
				boost::json::object result;
				for (const auto& key : keys)
				{
					result[key] = sortedValue(value.as_object().at(key));
				}
				return result;
			}
			if (value.is_array())
			{
				boost::json::array result;
				for (const auto& item : value.as_array())
				{
					result.push_back(sortedValue(item));
				}
				return result;
			}
			return value;
		}
	} // namespace

	std::string canonicalJson(const boost::json::value& value)
	{
		return boost::json::serialize(sortedValue(value));
	}

	unsigned retryDelay(unsigned attempts, unsigned base, unsigned cap, std::uint64_t entropy)
	{
		if (base == 0 || cap < base)
		{
			throw std::invalid_argument("重试间隔参数无效");
		}
		unsigned delay = base;
		for (unsigned i = 1; i < attempts && delay < cap; ++i)
		{
			delay = delay > cap / 2 ? cap : std::min(cap, delay * 2);
		}
		const unsigned floor = std::max(1U, delay / 2);
		return floor + static_cast<unsigned>(entropy % (static_cast<std::uint64_t>(delay) - floor + 1));
	}

	std::int64_t unixSeconds()
	{
		return std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch())
			.count();
	}
} // namespace gamehub
