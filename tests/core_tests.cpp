/** @file core_tests.cpp
 * @brief 测试摘要、签名、订单字段和重试间隔的边界。
 */
#include "core/Crypto.h"
#include "core/Domain.h"
#include "core/GameAdapter.h"
#include "games/DemoGameAdapter.h"
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
} // namespace

int main()
{
	using namespace gamehub;
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
