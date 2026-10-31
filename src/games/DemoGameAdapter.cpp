/** @file DemoGameAdapter.cpp
 * @brief 演示订单用渠道订单号去重，换一个 bizKey 也不能重复到账。
 */
#include "DemoGameAdapter.h"
#include "core/Crypto.h"
#include <algorithm>
#include <limits>

namespace gamehub
{
	boost::json::object normalizeDemoOrder(const boost::json::object& body)
	{
		constexpr std::size_t kFieldCount = 7;
		if (body.size() != kFieldCount)
		{
			throw ApiError(400, "回调必须包含约定的七个字段");
		}
		auto bizKey = requiredString(body, "bizKey", kMaxKeyLength);
		auto playerId = requiredString(body, "playerId", kMaxKeyLength);
		auto productId = requiredString(body, "productId", kMaxKeyLength);
		auto currency = requiredString(body, "currency", 3);
		auto channelOrderId = requiredString(body, "channelOrderId", kMaxKeyLength);
		if (!validKey(bizKey) || !validKey(playerId) || !validKey(productId) || !validKey(channelOrderId)
			|| currency.size() != 3
			|| !std::all_of(currency.begin(),
							currency.end(),
							[](char c)
							{
								return c >= 'A' && c <= 'Z';
							}))
		{
			throw ApiError(400, "订单标识或币种无效");
		}
		return {{"bizKey", bizKey},
				{"playerId", playerId},
				{"productId", productId},
				{"currency", currency},
				{"channelOrderId", channelOrderId},
				{"serverId", requiredInt(body, "serverId", 1, std::numeric_limits<std::uint32_t>::max())},
				{"amountMinor", requiredInt(body, "amountMinor", 1, kMaxDemoAmountMinor)}};
	}

	std::string_view DemoGameAdapter::gameId() const
	{
		return config_.id;
	}

	Submission DemoGameAdapter::parseCallback(std::string_view channel,
											  const SignedRequest& request,
											  std::int64_t now) const
	{
		if (channel != "demo")
		{
			throw ApiError(404, "尚未支持此渠道");
		}
		if (!verifySignature(config_.callbackSecret,
							 request.timestamp,
							 request.signature,
							 signatureInput(request.timestamp, request.method, request.path, request.body),
							 now))
		{
			throw ApiError(401, "签名无效或请求已过期");
		}
		const auto order = normalizeDemoOrder(parseObject(request.body));
		const auto serverId = static_cast<std::uint32_t>(order.at("serverId").as_int64());
		config_.endpoint(serverId);
		Submission input;
		input.gameId = config_.id;
		input.taskType = "recharge_notify";
		input.nameSpace = "demo";
		input.bizKey = std::string(order.at("channelOrderId").as_string());
		if (order.at("bizKey").as_string() != input.bizKey)
		{
			throw ApiError(400, "演示渠道要求 bizKey 等于 channelOrderId");
		}
		input.payload = canonicalJson(order);
		input.fingerprint = sha256(input.payload);
		input.servers = {serverId};
		input.actor = "callback:demo";
		return input;
	}

	std::string DemoGameAdapter::signOutbound(const SignedRequest& request) const
	{
		return hmacSha256(config_.outboundSecret,
						  signatureInput(request.timestamp, request.method, request.path, request.body));
	}

	bool DemoGameAdapter::verifyInternalRequest(const SignedRequest& request, std::int64_t now) const
	{
		return verifySignature(config_.outboundSecret,
							   request.timestamp,
							   request.signature,
							   signatureInput(request.timestamp, request.method, request.path, request.body),
							   now);
	}
} // namespace gamehub
