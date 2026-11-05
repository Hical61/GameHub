/** @file core_tests.cpp
 * @brief 测试摘要、签名、订单字段和重试间隔的边界。
 */
#include "core/Crypto.h"
#include "core/Domain.h"
#include "core/GameAdapter.h"
#include "bootstrap/AdapterCatalog.h"
#include "games/DemoGameAdapter.h"
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>

namespace
{
	unsigned g_failures = 0;

	void check(bool value, const char* description)
	{
		if (!value)
		{
			++g_failures;
			std::cerr << "失败：" << description << '\n';
		}
	}

	template <class Callback>
	void rejects(Callback callback, const char* description)
	{
		try
		{
			callback();
		}
		catch (const gamehub::ApiError&)
		{
			return;
		}
		check(false, description);
	}

	template <class Callback>
	void rejectsConfig(Callback callback, const char* description)
	{
		try
		{
			callback();
		}
		catch (const gamehub::ConfigError&)
		{
			return;
		}
		check(false, description);
	}

	gamehub::GameConfig syntheticGame(std::string id = "synthetic")
	{
		return {std::move(id),
				std::string(32, 'a'),
				std::string(32, 'b'),
				{{1, "127.0.0.1", 9001, "/internal/gamehub"}}};
	}

	gamehub::Submission parseSigned(const gamehub::ChannelAdapter& adapter,
									const std::string& secret,
									std::string_view path,
									std::string_view body,
									std::string_view keyId = {})
	{
		const auto signature = gamehub::hmacSha256(secret, gamehub::signatureInput("1000", "POST", path, body, keyId));
		const gamehub::CallbackRequest request {"POST",
												path,
												path,
												body,
												[&signature, keyId](std::string_view name) -> std::string_view
												{
													if (name == "X-Key-Id")
													{
														return keyId;
													}
													return name == "X-Timestamp"   ? std::string_view("1000")
														   : name == "X-Signature" ? std::string_view(signature)
																				   : std::string_view {};
												}};
		return adapter.parseCallback(request, 1000);
	}

	void testBuiltinChannels()
	{
		using namespace gamehub;
		auto game = syntheticGame();
		Config config;
		config.games = {game};
		auto registry = createBuiltinCatalog().buildRegistry(config);
		const std::string body =
			R"({"bizKey":"synthetic-1","channelOrderId":"synthetic-1","serverId":1,"playerId":"synthetic-player","productId":"test-product","amountMinor":100,"currency":"CNY"})";
		const auto event = parseSigned(registry.channel(game.id, "demo"), game.callbackSecret, "/callback/demo", body);
		check(event.gameId == game.id && event.nameSpace == "demo" && event.actor == "callback:demo",
			  "旧格式保留业务作用域");
		check(event.payload == canonicalJson(parseObject(body)) && event.fingerprint == sha256(event.payload),
			  "旧格式摘要保持不变");
		const auto reply = registry.channel(game.id, "demo").reply({200, 42, true, {}});
		const auto accepted = parseObject(reply.body);
		check(reply.status == 200 && accepted.at("eventId").as_string() == "42" && accepted.at("duplicate").as_bool(),
			  "渠道自己编码受理应答");
		const auto unavailable = registry.channel(game.id, "demo").reply({503, 0, false, "synthetic-private-error"});
		check(unavailable.body.find("synthetic-private-error") == std::string::npos, "依赖异常详情不进入渠道应答");
		game.channels = {{"merchant-a", "demo-hmac-v1", "merchant:a", std::string(32, 'c'), {}},
						 {"merchant-b", "demo-hmac-v1", "merchant:b", std::string(32, 'd'), {}}};
		config.games = {game};
		registry = createBuiltinCatalog().buildRegistry(config);
		const auto first = parseSigned(registry.channel(game.id, "merchant-a"), game.channels[0].secret, "/a", body);
		const auto second = parseSigned(registry.channel(game.id, "merchant-b"), game.channels[1].secret, "/b", body);
		check(first.bizKey == second.bizKey && first.nameSpace != second.nameSpace, "同号订单按商户作用域隔离");
		rejects(
			[&]
			{
				parseSigned(registry.channel(game.id, "merchant-b"), game.channels[0].secret, "/b", body);
			},
			"商户密钥不串用");
		rejects(
			[&]
			{
				registry.channel(game.id, "demo");
			},
			"显式渠道配置不再隐式开放旧渠道");
		game.adapter = "unknown-protocol";
		config.games = {game};
		rejectsConfig(
			[&]
			{
				createBuiltinCatalog().validate(config);
			},
			"目录拒绝未知游戏类型");
		game.adapter = "demo-hmac-v1";
		game.channels[0].adapter = "unknown-protocol";
		config.games = {game};
		rejectsConfig(
			[&]
			{
				createBuiltinCatalog().validate(config);
			},
			"目录拒绝未知渠道类型");
	}

	void testGameRulesSurviveChannelSelection()
	{
		using namespace gamehub;
		auto game = syntheticGame("synthetic-policy");
		game.adapter = "synthetic-strict-v1";
		game.channels = {{"merchant-a", "demo-hmac-v1", "merchant:a", game.callbackSecret, {}},
						 {"merchant-b", "demo-hmac-v1", "merchant:b", game.callbackSecret, {}}};
		GameRegistry registry;
		{
			auto catalog = createBuiltinCatalog();
			catalog.addGame(
				"synthetic-strict-v1",
				[](const GameConfig& config)
				{
					return std::make_unique<DemoGameAdapter>(config);
				},
				[](const boost::json::object& body)
				{
					auto order = normalizeDemoOrder(body);
					if (order.at("productId").as_string() != "synthetic-allowed")
					{
						throw ApiError(400, "合成策略只接受指定测试商品");
					}
					return order;
				},
				"demo");
			Config config;
			config.games = {game};
			registry = catalog.buildRegistry(config);
		}
		const auto order = parseObject(
			R"({"bizKey":"synthetic-policy-order","channelOrderId":"synthetic-policy-order","serverId":1,"playerId":"synthetic-player","productId":"synthetic-allowed","amountMinor":100,"currency":"USD"})");
		for (const auto* channelId : {"merchant-a", "merchant-b"})
		{
			const auto& channel = registry.channel(game.id, channelId);
			const auto event = parseSigned(channel, game.callbackSecret, "/callback/synthetic", canonicalJson(order));
			check(event.gameId == game.id && event.payload == canonicalJson(order), "合成游戏可复用渠道并保留自身策略");
			auto invalid = order;
			invalid["productId"] = "synthetic-other";
			check(normalizeDemoOrder(invalid).size() == 7, "反例满足通用演示字段规则");
			rejects(
				[&]
				{
					parseSigned(channel, game.callbackSecret, "/callback/synthetic", canonicalJson(invalid));
				},
				"选择其他渠道实例仍需遵守所选游戏的合成策略");
		}
		check(registry.find(game.id).gameId() == game.id, "注册表不引用已销毁的目录与原配置");
	}

	class TextChannel final : public gamehub::ChannelAdapter
	{
	public:
		/// @param request 原始请求视图；now 未使用。@return 合成事件，验证不同渠道可以定义自己的输入协议。
		gamehub::Submission parseCallback(const gamehub::CallbackRequest& request, std::int64_t) const override
		{
			if (request.target != "/custom?scope=synthetic" || request.header("X-Custom") != "synthetic"
				|| request.body != "raw=synthetic+entity")
			{
				throw gamehub::ApiError(400, "合成协议输入错误");
			}
			gamehub::Submission event;
			event.bizKey = "synthetic-custom";
			return event;
		}

		/// @param result 受理状态。@return 该合成渠道要求的文本确认。
		gamehub::CallbackReply reply(const gamehub::CallbackResult& result) const override
		{
			return {result.status, "text/plain", result.status == 200 ? "SUCCESS" : "RETRY"};
		}
	};

	void testIndependentChannelProtocol()
	{
		using namespace gamehub;
		auto catalog = createBuiltinCatalog();
		catalog.addChannel("synthetic-text-v1",
						   [](const GameConfig&, const ChannelConfig&, const AdapterCatalog::PaymentNormalizer&)
						   {
							   return std::make_unique<TextChannel>();
						   });
		auto game = syntheticGame();
		game.channels = {{"custom", "synthetic-text-v1", "synthetic", {}, {}}};
		Config config;
		config.games = {game};
		auto registry = catalog.buildRegistry(config);
		CallbackRequest request {"POST",
								 "/custom",
								 "/custom?scope=synthetic",
								 "raw=synthetic+entity",
								 [](std::string_view name)
								 {
									 return name == "X-Custom" ? std::string_view("synthetic") : std::string_view {};
								 }};
		const auto& channel = registry.channel(game.id, "custom");
		check(channel.parseCallback(request, 1000).bizKey == "synthetic-custom",
			  "渠道可读取原始查询参数、正文及专有请求头");
		check(channel.reply({200, 42, false, {}}).body == "SUCCESS"
				  && channel.reply({503, 0, false, {}}).body == "RETRY",
			  "渠道成功失败应答独立于HTTP和存储实现");
	}

	std::string syntheticJwt(std::string_view encodedHeader)
	{
		return std::string(encodedHeader) + "." + "e30" + "." + "c2ln";
	}

	void testKeySelection()
	{
		using namespace gamehub;
		const std::string current(32, 'c');
		KeyRotation keys {"current", std::string(32, 'l'), {{"previous", std::string(32, 'p')}}};
		check(selectVerificationKey(current, keys, "current") == current, "明确标识选择当前密钥");
		check(selectVerificationKey(current, keys, "previous") == std::string(32, 'p'), "过渡窗口允许旧标识");
		check(selectVerificationKey(current, keys, "unknown").empty(), "未知标识不能降级为其他密钥");
		check(selectVerificationKey(current, keys, "") == std::string(32, 'l'), "无标识只走显式旧协议密钥");
		keys.legacySecret.clear();
		check(selectVerificationKey(current, keys, "").empty(), "未配置旧协议时拒绝无标识请求");
		keys.previousKeys.clear();
		check(selectVerificationKey(current, keys, "previous").empty(), "关闭旧窗口后不再接受旧标识");
		check(selectVerificationKey(current, {}, "") == current, "原单密钥配置保留兼容");
		check(selectVerificationKey(current, {}, "current").empty(), "原配置不接受任意标识绕过");
		check(readJwtKeyId(syntheticJwt("eyJhbGciOiJIUzI1NiIsImtpZCI6Im5ldyJ9")) == "new", "JWT仅提取头部密钥标识");
		check(readJwtKeyId(syntheticJwt("eyJhbGciOiJIUzI1NiJ9")).empty(), "无kid的JWT保留旧协议选择路径");
		for (const auto* header : {"!", "eyJraWQiOjF9", "eyJraWQiOiIifQ", "eyJraWQiOiIuLiJ9"})
		{
			rejects(
				[&]
				{
					readJwtKeyId(syntheticJwt(header));
				},
				"非法JWT头不能参与选密钥");
		}
		rejects(
			[&]
			{
				readJwtKeyId(syntheticJwt(std::string(1025, 'e')));
			},
			"JWT头解码前限制长度");
		rejects(
			[&]
			{
				readJwtKeyId(std::string("e30") + "." + "e30");
			},
			"JWT必须有三个段");
		rejects(
			[&]
			{
				readJwtKeyId(syntheticJwt("e30") + ".extra");
			},
			"JWT不接受多余段");
	}

	void testAdapterRotation()
	{
		using namespace gamehub;
		auto game = syntheticGame();
		game.callbackKeys = {"callback-new", std::string(32, 'l'), {{"callback-old", std::string(32, 'p')}}};
		game.outboundKeys = {"outbound-new", std::string(32, 'x'), {{"outbound-old", std::string(32, 'y')}}};
		Config config;
		config.games = {game};
		auto registry = createBuiltinCatalog().buildRegistry(config);
		const auto& channel = registry.channel(game.id, "demo");
		const std::string body =
			R"({"bizKey":"rotate","channelOrderId":"rotate","serverId":1,"playerId":"synthetic-player","productId":"test-product","amountMinor":100,"currency":"CNY"})";
		const auto current = parseSigned(channel, game.callbackSecret, "/callback/demo", body, "callback-new");
		const auto previous = parseSigned(channel, std::string(32, 'p'), "/callback/demo", body, "callback-old");
		const auto legacy = parseSigned(channel, std::string(32, 'l'), "/callback/demo", body);
		check(current.fingerprint == previous.fingerprint && previous.fingerprint == legacy.fingerprint,
			  "轮换不改变业务身份和摘要");
		rejects(
			[&]
			{
				parseSigned(channel, game.callbackSecret, "/callback/demo", body, "unknown");
			},
			"未知渠道kid即使用当前密钥签名也拒绝");
		rejects(
			[&]
			{
				parseSigned(channel, game.callbackSecret, "/callback/demo", body);
			},
			"当前渠道密钥不能绕过显式legacy路径");
		const auto& adapter = registry.find(game.id);
		SignedRequest request {"1000", {}, "GET", "/internal/config/synthetic/1/0", "", "outbound-new"};
		const auto signature = adapter.signOutbound(request);
		request.signature = signature;
		check(adapter.outboundKeyId() == "outbound-new" && adapter.verifyInternalRequest(request, 1000),
			  "出向使用当前标识且内部请求接受当前签名");
		request.keyId = {};
		check(!adapter.verifyInternalRequest(request, 1000), "删除出向kid不能降级通过");
		request.keyId = "outbound-old";
		const auto oldSignature =
			hmacSha256(std::string(32, 'y'),
					   signatureInput(request.timestamp, request.method, request.path, request.body, request.keyId));
		request.signature = oldSignature;
		check(adapter.verifyInternalRequest(request, 1000), "旧内部请求在窗口内可验证");
		request.keyId = "unknown";
		check(!adapter.verifyInternalRequest(request, 1000), "未知内部kid不能回退尝试旧密钥");
		config.games[0].callbackKeys.legacySecret.clear();
		config.games[0].callbackKeys.previousKeys.clear();
		registry = createBuiltinCatalog().buildRegistry(config);
		rejects(
			[&]
			{
				parseSigned(registry.channel(game.id, "demo"), std::string(32, 'l'), "/callback/demo", body);
			},
			"关闭legacy窗口后旧无标识请求失效");
		rejects(
			[&]
			{
				parseSigned(registry.channel(game.id, "demo"),
							std::string(32, 'p'),
							"/callback/demo",
							body,
							"callback-old");
			},
			"移除旧密钥后旧kid失效");
	}

	struct ConfigFile
	{
		std::filesystem::path path =
			std::filesystem::temp_directory_path()
			/ ("gamehub-config-test-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count())
			   + ".json");

		~ConfigFile()
		{
			std::error_code error;
			std::filesystem::remove(path, error);
		}

		gamehub::Config load(const boost::json::object& object)
		{
			{
				std::ofstream file(path, std::ios::binary | std::ios::trunc);
				file << boost::json::serialize(object);
				if (!file)
				{
					throw std::runtime_error("合成配置写入失败");
				}
			}
			return gamehub::loadConfig(path);
		}
	};

	void setSyntheticEnvironment(const char* name)
	{
		const std::string value(32, 's');
#ifdef _WIN32
		if (_putenv_s(name, value.c_str()) != 0)
#else
		if (setenv(name, value.c_str(), 1) != 0)
#endif
		{
			throw std::runtime_error("合成环境变量设置失败");
		}
	}

	void testRotationConfig(ConfigFile& file, boost::json::object rotating)
	{
		auto& auth = rotating.at("auth").as_object();
		auth["jwtKeyId"] = "jwt-new";
		auth["jwtPreviousKeys"] = boost::json::array {
			boost::json::object {{"keyId", "jwt-old"}, {"secretEnv", "GAMEHUB_SYNTHETIC_CONFIG_SECRET"}}};
		auth["jwtLegacySecretEnv"] = "GAMEHUB_SYNTHETIC_CONFIG_SECRET";
		auto& game = rotating.at("games").as_array()[0].as_object();
		game["outboundKeyId"] = "outbound-new";
		auto& channel = game.at("channels").as_array()[0].as_object();
		channel["keyId"] = "callback-new";
		channel["legacySecretEnv"] = "GAMEHUB_SYNTHETIC_CONFIG_SECRET";
		const auto config = file.load(rotating);
		check(config.jwtKeys.currentKeyId == "jwt-new" && config.jwtKeys.previousKeys.size() == 1
				  && !config.jwtKeys.legacySecret.empty() && config.games[0].outboundKeys.currentKeyId == "outbound-new"
				  && config.games[0].channels[0].keys.currentKeyId == "callback-new",
			  "三类轮换配置正确读取");
		auth["jwtPreviousKeys"].as_array()[0].as_object()["keyId"] = "jwt-new";
		rejectsConfig(
			[&]
			{
				file.load(rotating);
			},
			"拒绝当前和旧密钥标识重复");
		auth.erase("jwtKeyId");
		rejectsConfig(
			[&]
			{
				file.load(rotating);
			},
			"旧密钥配置不能省略当前密钥标识");
	}

	void testConfigCompatibility()
	{
		using namespace gamehub;
		setSyntheticEnvironment("GAMEHUB_HUBDB_PASSWORD");
		setSyntheticEnvironment("GAMEHUB_SYNTHETIC_CONFIG_SECRET");
		auto json = parseObject(
			R"({"server":{"listen":"127.0.0.1","port":8443,"maxBodyBytes":65536,"maxConnections":1024},"hubDb":{"host":"127.0.0.1","port":3306,"database":"synthetic_hub","user":"synthetic_runtime","maxConnections":8},"worker":{"pollMs":1000,"concurrency":4,"leaseSeconds":60,"maxAttempts":8,"baseRetrySeconds":2,"maxRetrySeconds":300,"connectTimeoutMs":2000,"requestTimeoutMs":5000},"auth":{"jwtSecretEnv":"GAMEHUB_SYNTHETIC_CONFIG_SECRET","issuer":"synthetic","audience":"synthetic"},"games":[{"id":"synthetic","adapter":"demo-hmac-v1","callbackSecretEnv":"GAMEHUB_SYNTHETIC_CONFIG_SECRET","outboundSecretEnv":"GAMEHUB_SYNTHETIC_CONFIG_SECRET","servers":[{"id":1,"host":"127.0.0.1","port":9001,"target":"/internal/gamehub"}]}]})");
		ConfigFile file;
		auto config = file.load(json);
		createBuiltinCatalog().validate(config);
		check(config.games[0].channels.empty() && config.worker.games.empty(), "旧配置无需新增字段即可使用");
		auto& game = json.at("games").as_array()[0].as_object();
		game.erase("callbackSecretEnv");
		game["channels"] = boost::json::array {boost::json::object {{"id", "merchant-a"},
																	{"adapter", "demo-hmac-v1"},
																	{"namespace", "merchant:a"},
																	{"secretEnv", "GAMEHUB_SYNTHETIC_CONFIG_SECRET"}}};
		game["deliveryEnabled"] = false;
		game["maxConcurrentDeliveries"] = 2;
		game["maxConcurrentPerServer"] = 1;
		game["callbackRatePerSecond"] = 50;
		game["callbackBurst"] = 80;
		game["options"] = boost::json::object {{"syntheticSchema", "v1"}};
		json.at("worker").as_object()["games"] = boost::json::array {"synthetic"};
		config = file.load(json);
		createBuiltinCatalog().validate(config);
		check(config.games[0].channels[0].nameSpace == "merchant:a" && config.games[0].callbackSecret.empty(),
			  "显式渠道独立配置凭据作用域");
		check(!config.games[0].deliveryEnabled && config.games[0].maxConcurrentDeliveries == 2
				  && config.games[0].maxConcurrentPerServer == 1 && config.games[0].callbackRatePerSecond == 50
				  && config.games[0].callbackBurst == 80
				  && config.worker.games == std::vector<std::string> {"synthetic"},
			  "运行隔离字段正确读取");
		check(config.games[0].options.at("syntheticSchema").as_string() == "v1", "专用配置保留给适配器工厂检查");
		testRotationConfig(file, json);
		json.at("worker").as_object()["games"] = boost::json::array {"missing"};
		rejectsConfig(
			[&]
			{
				file.load(json);
			},
			"拒绝Worker领取未配置游戏");
		json.at("worker").as_object()["games"] = boost::json::array {"synthetic", "synthetic"};
		rejectsConfig(
			[&]
			{
				file.load(json);
			},
			"拒绝Worker重复游戏范围");
		json.at("worker").as_object()["games"] = boost::json::array {};
		game["adapter"] = "synthetic-unregistered";
		config = file.load(json);
		rejectsConfig(
			[&]
			{
				createBuiltinCatalog().validate(config);
			},
			"启动目录统一检查未知类型");
		game["adapter"] = "demo-hmac-v1";
		game["deliveryEnabled"] = "false";
		rejectsConfig(
			[&]
			{
				file.load(json);
			},
			"暂停字段拒绝字符串布尔值");
	}
} // namespace

int main()
{
	using namespace gamehub;
	testBuiltinChannels();
	testGameRulesSurviveChannelSelection();
	testIndependentChannelProtocol();
	testKeySelection();
	testAdapterRotation();
	testConfigCompatibility();
	check(sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "SHA-256 标准向量");
	check(hmacSha256("key", "The quick brown fox jumps over the lazy dog")
			  == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
		  "HMAC-SHA256 标准向量");
	const std::string secret(32, 'x');
	const auto input = signatureInput("1000", "POST", "/api/callback/demo/demo", "{}");
	const auto signature = hmacSha256(secret, input);
	check(verifySignature(secret, "1000", signature, input, 1000), "有效签名");
	check(verifySignature(secret, "1000", signature, input, 1300), "允许时间窗边界");
	check(!verifySignature(secret, "1000", signature, input, 1301), "拒绝过期请求");
	check(!verifySignature(secret, "1000", signature, input, 699), "拒绝过远未来时间");
	check(!verifySignature(secret, "1000x", signature, input, 1000), "拒绝非完整整数");
	check(!verifySignature(secret, "1000", signature, signatureInput("1000", "POST", "/other", "{}"), 1000),
		  "路径不可替换");
	check(!verifySignature(secret,
						   "1000",
						   signature,
						   signatureInput("1000", "POST", "/api/callback/demo/demo", "{ }"),
						   1000),
		  "原始实体字节不可替换");
	check(!secureEqual("a", "b") && !secureEqual("a", "aa") && secureEqual("abc", "abc"), "恒定时间比较语义");
	check(canonicalJson(parseObject(R"({"b":2,"a":{"z":0,"x":1}})")) == R"({"a":{"x":1,"z":0},"b":2})", "嵌套对象排序");
	rejects(
		[]
		{
			parseObject("[]");
		},
		"拒绝非对象 JSON");
	rejects(
		[]
		{
			parseObject("{");
		},
		"拒绝畸形 JSON");
	auto order = parseObject(
		R"({"bizKey":"test-1","channelOrderId":"test-1","serverId":1,"playerId":"synthetic-player","productId":"test-product","amountMinor":100,"currency":"CNY"})");
	check(normalizeDemoOrder(order).size() == 7, "接受完整演示订单");
	order["amountMinor"] = 0;
	rejects(
		[&]
		{
			normalizeDemoOrder(order);
		},
		"拒绝零金额");
	order["amountMinor"] = 1.5;
	rejects(
		[&]
		{
			normalizeDemoOrder(order);
		},
		"拒绝浮点金额");
	order["amountMinor"] = std::numeric_limits<std::uint64_t>::max();
	rejects(
		[&]
		{
			normalizeDemoOrder(order);
		},
		"拒绝无符号溢出金额");
	order["amountMinor"] = 100;
	order["serverId"] = -1;
	rejects(
		[&]
		{
			normalizeDemoOrder(order);
		},
		"拒绝负区服");
	check(!validKey("") && !validKey("x\ny") && !validKey("中文") && validKey("a:_.-09"), "ASCII 业务键边界");
	GameConfig gameA {"demo",
					  std::string(32, 'a'),
					  std::string(32, 'b'),
					  {{1, "127.0.0.1", 9001, "/internal/gamehub"}}};
	GameConfig gameB {"other",
					  std::string(32, 'c'),
					  std::string(32, 'd'),
					  {{1, "127.0.0.1", 9002, "/internal/gamehub"}}};
	GameRegistry registry;
	registry.add(std::make_unique<DemoGameAdapter>(gameA));
	registry.add(std::make_unique<DemoGameAdapter>(gameB));
	check(registry.find("demo").gameId() == "demo" && registry.find("other").gameId() == "other", "注册表游戏隔离");
	rejects(
		[&]
		{
			registry.find("unknown");
		},
		"拒绝未注册游戏");
	SignedRequest outbound {"1000", {}, "GET", "/internal/config/demo/1/0", ""};
	auto signedByA = registry.find("demo").signOutbound(outbound);
	outbound.signature = signedByA;
	check(registry.find("demo").verifyInternalRequest(outbound, 1000), "本游戏签名可验证");
	check(!registry.find("other").verifyInternalRequest(outbound, 1000), "不同游戏密钥隔离");
	for (unsigned attempt : {0U, 1U, 2U, 8U, 100U, UINT_MAX})
	{
		const auto delay = retryDelay(attempt, 2, 300, UINT64_MAX);
		check(delay >= 1 && delay <= 300, "退避含上限且不溢出");
	}
	check(retryDelay(UINT_MAX, UINT_MAX / 2, UINT_MAX, UINT64_MAX) <= UINT_MAX, "极大退避不溢出");
	if (g_failures == 0)
	{
		std::cout << "核心测试全部通过\n";
	}
	return g_failures == 0 ? 0 : 1;
}
