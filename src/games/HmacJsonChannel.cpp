/** @file HmacJsonChannel.cpp
 * @brief 共享 HMAC 入口协议，不依赖 HTTP 服务、数据库或游戏经济写入。
 */
#include "HmacJsonChannel.h"
#include "core/Crypto.h"

namespace gamehub
{
	HmacJsonChannel::HmacJsonChannel(const GameConfig& game, ChannelConfig channel, Normalizer normalize)
		: game_(game), channel_(std::move(channel)), normalize_(std::move(normalize))
	{
		if (!normalize_ || channel_.secret.size() < 32 || !validKey(channel_.nameSpace, 32))
		{
			throw ConfigError("渠道凭据、命名空间或规范化函数无效");
		}
	}

	Submission HmacJsonChannel::parseCallback(const CallbackRequest& request, std::int64_t now) const
	{
		if (request.method != "POST" || request.target != request.path)
		{
			throw ApiError(400, "内置 HMAC 通知只接受无查询参数的 POST 请求");
		}
		if (!request.header)
		{
			throw ApiError(401, "签名无效或请求已过期");
		}
		const auto timestamp = request.header("X-Timestamp");
		if (!verifySignature(channel_.secret,
							 timestamp,
							 request.header("X-Signature"),
							 signatureInput(timestamp, request.method, request.path, request.body),
							 now))
		{
			throw ApiError(401, "签名无效或请求已过期");
		}
		const auto order = normalize_(parseObject(request.body));
		const auto serverId = static_cast<std::uint32_t>(order.at("serverId").as_int64());
		game_.endpoint(serverId);
		Submission input;
		input.gameId = game_.id;
		input.taskType = "recharge_notify";
		input.nameSpace = channel_.nameSpace;
		input.bizKey = std::string(order.at("channelOrderId").as_string());
		if (order.at("bizKey").as_string() != input.bizKey)
		{
			throw ApiError(400, "内部通知要求 bizKey 等于 channelOrderId");
		}
		input.payload = canonicalJson(order);
		input.fingerprint = sha256(input.payload);
		input.servers = {serverId};
		input.actor = "callback:" + channel_.id;
		return input;
	}

	CallbackReply HmacJsonChannel::reply(const CallbackResult& result) const
	{
		boost::json::object body;
		if (result.status == 200)
		{
			body = {{"eventId", std::to_string(result.eventId)}, {"duplicate", result.duplicate}, {"accepted", true}};
		}
		else
		{
			body = {{"error", result.status >= 500 ? "依赖服务暂时不可用，请保留原业务键重试" : result.message}};
		}
		return {result.status, "application/json; charset=utf-8", boost::json::serialize(body)};
	}
} // namespace gamehub
