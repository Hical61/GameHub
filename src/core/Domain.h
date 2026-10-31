/** @file Domain.h
 * @brief 业务数据、任务状态和重试间隔。
 */
#pragma once
#include <boost/json.hpp>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace gamehub
{
	inline constexpr std::size_t kMaxKeyLength = 96;
	inline constexpr std::size_t kMaxPayloadBytes = 48 * 1024;
	inline constexpr int kSignatureWindowSeconds = 300;

	enum class TaskStatus : int
	{
		Pending,
		Delivering,
		Done,
		Failed
	};

	class ApiError : public std::runtime_error
	{
	public:
		/// @param status HTTP 状态码；message 返回给调用方的错误说明。
		ApiError(int status, const std::string& message) : std::runtime_error(message), status(status)
		{
		}

		int status;
	};

	struct Submission
	{
		std::string gameId;
		std::string taskType;
		std::string nameSpace;
		std::string bizKey;
		std::string payload;
		std::string fingerprint;
		std::vector<std::uint32_t> servers;
		std::string actor;
	};

	struct DeliveryTask
	{
		std::uint64_t id = 0;
		std::uint64_t generation = 0;
		std::string gameId;
		std::uint32_t serverId = 0;
		std::string taskType;
		std::string bizKey;
		std::string payload;
		unsigned attempts = 0;
	};

	/// @param text 待检查的标识；maxLength 长度上限。@return 是否满足 ASCII 标识的字符和长度限制。
	bool validKey(std::string_view text, std::size_t maxLength = kMaxKeyLength);
	/// @param object JSON 对象；key 字段名；maxLength 字节上限。@return 非空字符串；字段缺失或不合法时抛 ApiError。
	std::string requiredString(const boost::json::object& object, std::string_view key, std::size_t maxLength);
	/// @param object JSON 对象；key 字段名；min/max 允许范围。@return 范围内的整数；字段缺失或不合法时抛 ApiError。
	std::int64_t requiredInt(const boost::json::object& object,
							 std::string_view key,
							 std::int64_t min,
							 std::int64_t max);
	/// @param body JSON 字符串。@return 解析后的对象；格式不对或不是对象时抛 ApiError。
	boost::json::object parseObject(std::string_view body);
	/// @param value JSON 值。@return 按对象键排序后的 JSON 字符串，用于计算业务幂等摘要。
	std::string canonicalJson(const boost::json::value& value);
	/// @param attempts 已经开始的尝试次数；base/cap 基础间隔和上限，单位秒；entropy 抖动源。@return 加入抖动后的重试间隔，单位秒。
	unsigned retryDelay(unsigned attempts, unsigned base, unsigned cap, std::uint64_t entropy);
	/// @return 当前 UTC Unix 秒数。
	std::int64_t unixSeconds();
} // namespace gamehub
