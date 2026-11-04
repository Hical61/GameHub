/** @file Config.cpp
 * @brief 读取配置，检查游戏、区服和 HTTP 目标地址。
 */
#include "Config.h"
#include "Domain.h"
#include <boost/asio/ip/address.hpp>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <set>

namespace gamehub
{
	namespace
	{
		std::string secretFromEnv(const std::string& name, std::size_t minLength)
		{
			if (!validKey(name))
			{
				throw ConfigError("环境变量名称无效");
			}
			const char* value = std::getenv(name.c_str());
			if (!value || std::string_view(value).size() < minLength)
			{
				throw ConfigError("环境变量未设置或长度不够：" + name);
			}
			return value;
		}

		const boost::json::object& objectAt(const boost::json::object& root, std::string_view key)
		{
			auto* value = root.if_contains(key);
			if (!value || !value->is_object())
			{
				throw ConfigError("配置中缺少对象：" + std::string(key));
			}
			return value->as_object();
		}

		unsigned optionalUnsigned(const boost::json::object& object,
								  std::string_view key,
								  unsigned fallback,
								  unsigned minimum,
								  unsigned maximum)
		{
			return object.if_contains(key) ? static_cast<unsigned>(requiredInt(object, key, minimum, maximum))
										   : fallback;
		}

		std::vector<std::string> parseWorkerGames(const boost::json::object& object)
		{
			std::vector<std::string> games;
			const auto* selected = object.if_contains("games");
			if (!selected)
			{
				return games;
			}
			if (!selected->is_array() || selected->as_array().size() > 100)
			{
				throw ConfigError("worker.games 必须是至多 100 个游戏标识的数组");
			}
			std::set<std::string> ids;
			for (const auto& value : selected->as_array())
			{
				if (!value.is_string() || !validKey(value.as_string(), 32)
					|| !ids.insert(std::string(value.as_string())).second)
				{
					throw ConfigError("worker.games 包含非法或重复游戏标识");
				}
				games.emplace_back(value.as_string());
			}
			return games;
		}

		WorkerConfig parseWorker(const boost::json::object& object)
		{
			WorkerConfig worker;
			worker.games = parseWorkerGames(object);
			worker.pollMs = static_cast<unsigned>(requiredInt(object, "pollMs", 10, 60000));
			worker.concurrency = static_cast<unsigned>(requiredInt(object, "concurrency", 1, 32));
			worker.leaseSeconds = static_cast<unsigned>(requiredInt(object, "leaseSeconds", 10, 3600));
			worker.maxAttempts = static_cast<unsigned>(requiredInt(object, "maxAttempts", 1, 100));
			worker.baseRetrySeconds = static_cast<unsigned>(requiredInt(object, "baseRetrySeconds", 1, 3600));
			worker.maxRetrySeconds =
				static_cast<unsigned>(requiredInt(object, "maxRetrySeconds", worker.baseRetrySeconds, 86400));
			worker.connectTimeoutMs = static_cast<unsigned>(requiredInt(object, "connectTimeoutMs", 100, 10000));
			worker.requestTimeoutMs = static_cast<unsigned>(requiredInt(object, "requestTimeoutMs", 100, 30000));
			if (object.if_contains("maxResponseBodyBytes"))
			{
				constexpr std::int64_t kMinResponseBodyBytes = 64;
				constexpr std::int64_t kMaxResponseBodyBytes = 1024 * 1024;
				worker.maxResponseBodyBytes = static_cast<std::size_t>(
					requiredInt(object, "maxResponseBodyBytes", kMinResponseBodyBytes, kMaxResponseBodyBytes));
			}
			// HTTP 连接池可能重发一次。领取后的提交，以及确认时借连接和更新，另留 20 秒。
			if (worker.leaseSeconds * 1000 <= 2 * (worker.connectTimeoutMs + worker.requestTimeoutMs) + 20000)
			{
				throw ConfigError("租约时间不够，需要容纳两次 HTTP 请求和数据库确认操作");
			}
			return worker;
		}

		ChannelConfig parseChannel(const boost::json::object& object)
		{
			ChannelConfig channel;
			channel.id = requiredString(object, "id", 32);
			channel.adapter = requiredString(object, "adapter", 32);
			channel.nameSpace = requiredString(object, "namespace", 32);
			if (!validKey(channel.id, 32) || channel.id == "." || channel.id == ".." || !validKey(channel.adapter, 32)
				|| !validKey(channel.nameSpace, 32))
			{
				throw ConfigError("渠道实例、类型或命名空间无效");
			}
			if (object.if_contains("secretEnv"))
			{
				channel.secret = secretFromEnv(requiredString(object, "secretEnv", 96), 32);
			}
			if (object.if_contains("options"))
			{
				channel.options = objectAt(object, "options");
			}
			return channel;
		}

		void parseGameOptions(GameConfig& game, const boost::json::object& object)
		{
			if (const auto* enabled = object.if_contains("deliveryEnabled"))
			{
				if (!enabled->is_bool())
				{
					throw ConfigError("deliveryEnabled 必须是布尔值");
				}
				game.deliveryEnabled = enabled->as_bool();
			}
			game.maxConcurrentDeliveries = optionalUnsigned(object, "maxConcurrentDeliveries", 0, 0, 32);
			game.maxConcurrentPerServer = optionalUnsigned(object, "maxConcurrentPerServer", 0, 0, 32);
			game.callbackRatePerSecond = optionalUnsigned(object, "callbackRatePerSecond", 100, 1, 100000);
			game.callbackBurst = optionalUnsigned(object, "callbackBurst", 200, 1, 100000);
			if (object.if_contains("options"))
			{
				game.options = objectAt(object, "options");
			}
			const auto* channels = object.if_contains("channels");
			if (!channels)
			{
				game.callbackSecret = secretFromEnv(requiredString(object, "callbackSecretEnv", 96), 32);
				return;
			}
			if (!channels->is_array() || channels->as_array().empty() || channels->as_array().size() > 32)
			{
				throw ConfigError("channels 必须包含 1 至 32 个渠道实例");
			}
			std::set<std::string> ids;
			for (const auto& value : channels->as_array())
			{
				auto channel = parseChannel(value.as_object());
				if (!ids.insert(channel.id).second)
				{
					throw ConfigError("渠道实例标识重复");
				}
				game.channels.push_back(std::move(channel));
			}
		}

		GameConfig parseGame(const boost::json::object& object)
		{
			GameConfig game;
			game.id = requiredString(object, "id", 32);
			game.adapter = requiredString(object, "adapter", 32);
			if (!validKey(game.id, 32) || game.id == "." || game.id == ".." || !validKey(game.adapter, 32))
			{
				throw ConfigError("游戏或适配器标识无效");
			}
			parseGameOptions(game, object);
			if (object.if_contains("outboundSecretEnv"))
			{
				game.outboundSecret = secretFromEnv(requiredString(object, "outboundSecretEnv", 96), 32);
			}
			const auto& servers = object.at("servers").as_array();
			if (servers.empty() || servers.size() > 1000)
			{
				throw ConfigError("每款游戏必须配置 1 至 1000 个区服");
			}
			std::set<std::uint32_t> ids;
			for (const auto& item : servers)
			{
				const auto& server = item.as_object();
				ServerEndpoint endpoint;
				endpoint.id = static_cast<std::uint32_t>(requiredInt(server, "id", 1, UINT32_MAX));
				endpoint.host = requiredString(server, "host", 253);
				endpoint.port = static_cast<std::uint16_t>(requiredInt(server, "port", 1, UINT16_MAX));
				endpoint.target = requiredString(server, "target", 256);
				if (!ids.insert(endpoint.id).second || endpoint.target.front() != '/'
					|| endpoint.target.find_first_of("\r\n ?#") != std::string::npos
					|| endpoint.host.find_first_of("\r\n /?#@") != std::string::npos)
				{
					throw ConfigError("区服编号重复，或目标地址格式不对");
				}
				game.servers.push_back(std::move(endpoint));
			}
			return game;
		}
	} // namespace

	Config loadConfig(const std::filesystem::path& path)
	{
		std::ifstream file(path, std::ios::binary);
		if (!file)
		{
			throw ConfigError("无法读取配置文件");
		}
		constexpr std::uintmax_t kMaxConfigBytes = 1024 * 1024;
		if (std::filesystem::file_size(path) > kMaxConfigBytes)
		{
			throw ConfigError("配置文件超过 1 MiB");
		}
		std::string bytes {std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
		auto root = parseObject(bytes);
		const auto& server = objectAt(root, "server");
		const auto& db = objectAt(root, "hubDb");
		const auto& auth = objectAt(root, "auth");
		Config result;
		result.listen = requiredString(server, "listen", 64);
		boost::system::error_code addressError;
		const auto listenAddress = boost::asio::ip::make_address(result.listen, addressError);
		if (addressError)
		{
			throw ConfigError("监听地址必须是有效的 IP");
		}
		// 用 HTTP 服务的 IP 解析方式判断，同时处理完整、压缩和 IPv4 映射地址。
		const bool mappedWildcard =
			listenAddress.is_v6() && listenAddress.to_v6().is_v4_mapped()
			&& boost::asio::ip::make_address_v4(boost::asio::ip::v4_mapped, listenAddress.to_v6()).is_unspecified();
		if (listenAddress.is_unspecified() || mappedWildcard)
		{
			throw ConfigError("请指定内网或回环 IP，不能监听全部地址");
		}
		result.port = static_cast<std::uint16_t>(requiredInt(server, "port", 1, UINT16_MAX));
		result.maxBodyBytes = static_cast<std::size_t>(requiredInt(server, "maxBodyBytes", 1024, 65536));
		result.maxConnections = static_cast<std::size_t>(requiredInt(server, "maxConnections", 1, 100000));
		result.dbHost = requiredString(db, "host", 253);
		result.dbPort = static_cast<std::uint16_t>(requiredInt(db, "port", 1, UINT16_MAX));
		result.dbName = requiredString(db, "database", 64);
		result.dbUser = requiredString(db, "user", 64);
		result.dbPassword = secretFromEnv("GAMEHUB_HUBDB_PASSWORD", 1);
		result.dbMaxConnections = static_cast<unsigned>(requiredInt(db, "maxConnections", 2, 128));
		result.jwtSecret = secretFromEnv(requiredString(auth, "jwtSecretEnv", 96), 32);
		result.jwtIssuer = requiredString(auth, "issuer", 96);
		result.jwtAudience = requiredString(auth, "audience", 96);
		result.worker = parseWorker(objectAt(root, "worker"));
		std::set<std::string> ids;
		const auto& games = root.at("games").as_array();
		if (games.empty() || games.size() > 100)
		{
			throw ConfigError("必须配置 1 至 100 款游戏");
		}
		for (const auto& item : games)
		{
			auto game = parseGame(item.as_object());
			if (!ids.insert(game.id).second)
			{
				throw ConfigError("游戏标识重复");
			}
			result.games.push_back(std::move(game));
		}
		for (const auto& selected : result.worker.games)
		{
			if (!ids.contains(selected))
			{
				throw ConfigError("worker.games 引用了未配置的游戏");
			}
		}
		return result;
	}

	const GameConfig& Config::game(std::string_view gameId) const
	{
		for (const auto& item : games)
		{
			if (item.id == gameId)
			{
				return item;
			}
		}
		throw ApiError(404, "游戏不存在");
	}

	const ServerEndpoint& GameConfig::endpoint(std::uint32_t serverId) const
	{
		for (const auto& item : servers)
		{
			if (item.id == serverId)
			{
				return item;
			}
		}
		throw ApiError(404, "区服不存在");
	}
} // namespace gamehub
