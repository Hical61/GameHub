/** @file Config.h
 * @brief 服务配置、游戏和区服地址。
 */
#pragma once
#include <boost/json.hpp>
#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace gamehub
{
	inline constexpr std::size_t kMaxKeyIdLength = 64;
	inline constexpr std::size_t kMaxPreviousVerificationKeys = 8;

	struct VerificationKey
	{
		std::string keyId;
		std::string secret;
	};

	struct KeyRotation
	{
		std::string currentKeyId;
		// 仅用于无 key-id 的旧协议；启用轮换后，不默认使用当前密钥验证无 key-id 请求。
		std::string legacySecret;
		std::vector<VerificationKey> previousKeys;
	};

	/// @param currentSecret 当前密钥；rotation 轮换窗口；keyId 请求指定的密钥标识。@return 唯一选中的密钥；未知标识或未开放旧协议时返回空视图。
	std::string_view selectVerificationKey(std::string_view currentSecret,
										   const KeyRotation& rotation,
										   std::string_view keyId);

	class ConfigError : public std::runtime_error
	{
	public:
		/// @param message 错误说明，不能包含配置值或密钥。
		explicit ConfigError(const std::string& message) : std::runtime_error(message)
		{
		}
	};

	struct ServerEndpoint
	{
		std::uint32_t id = 0;
		std::string host;
		std::uint16_t port = 0;
		std::string target;
	};

	struct ChannelConfig
	{
		std::string id;
		std::string adapter;
		std::string nameSpace;
		std::string secret;
		boost::json::object options;
		KeyRotation keys;
	};

	struct GameConfig
	{
		std::string id;
		std::string callbackSecret;
		std::string outboundSecret;
		std::vector<ServerEndpoint> servers;
		std::string adapter = "demo-hmac-v1";
		std::vector<ChannelConfig> channels;
		bool deliveryEnabled = true;
		unsigned maxConcurrentDeliveries = 0;
		unsigned maxConcurrentPerServer = 0;
		unsigned callbackRatePerSecond = 100;
		unsigned callbackBurst = 200;
		boost::json::object options;
		KeyRotation callbackKeys;
		KeyRotation outboundKeys;
		/// @param serverId 区服编号。@return 对应的目标地址；未配置时抛 ApiError。
		const ServerEndpoint& endpoint(std::uint32_t serverId) const;
	};

	struct WorkerConfig
	{
		static constexpr std::size_t kDefaultMaxResponseBodyBytes = 16 * 1024;

		unsigned pollMs = 1000;
		unsigned concurrency = 4;
		unsigned leaseSeconds = 60;
		unsigned maxAttempts = 8;
		unsigned baseRetrySeconds = 2;
		unsigned maxRetrySeconds = 300;
		unsigned connectTimeoutMs = 2000;
		unsigned requestTimeoutMs = 5000;
		std::size_t maxResponseBodyBytes = kDefaultMaxResponseBodyBytes;
		// 空列表表示处理全部配置游戏；非空时领取及回收均仅处理这些游戏。
		std::vector<std::string> games;
		unsigned maxTasksPerTick = 16;
	};

	struct Config
	{
		std::string listen;
		std::uint16_t port = 0;
		std::size_t maxBodyBytes = 0;
		std::size_t maxConnections = 0;
		std::string dbHost;
		std::uint16_t dbPort = 0;
		std::string dbName;
		std::string dbUser;
		std::string dbPassword;
		unsigned dbMaxConnections = 0;
		std::string jwtSecret;
		std::string jwtIssuer;
		std::string jwtAudience;
		WorkerConfig worker;
		std::vector<GameConfig> games;
		KeyRotation jwtKeys;
		/// @param gameId 游戏标识。@return 对应的游戏配置；未配置时抛 ApiError。
		const GameConfig& game(std::string_view gameId) const;
	};

	/// @param path 标准 JSON 配置文件的路径。@return 通用结构检查通过的配置；协议专用检查由启动目录执行，密钥从环境变量读取。
	Config loadConfig(const std::filesystem::path& path);
} // namespace gamehub
