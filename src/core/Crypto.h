/** @file Crypto.h
 * @brief SHA-256 摘要和演示协议的 HMAC 签名。
 */
#pragma once
#include <cstdint>
#include <string>
#include <string_view>

namespace gamehub
{
	/// @param data 原始字节。@return 小写 SHA-256 十六进制摘要。
	std::string sha256(std::string_view data);
	/// @param secret 密钥；data 待签名字节。@return 小写 HMAC-SHA256 十六进制签名。
	std::string hmacSha256(std::string_view secret, std::string_view data);
	/// @param left/right 比较内容。@return 长度是否相等、内容是否一致；同长度内容使用恒定时间比较。
	bool secureEqual(std::string_view left, std::string_view right);
	/// @param timestamp Unix 秒数字符串；method HTTP 方法；path 原始路径；body 请求体字节。@return 待签名的字符串。
	std::string signatureInput(std::string_view timestamp,
							   std::string_view method,
							   std::string_view path,
							   std::string_view body);
	/// @param secret 密钥；timestamp 时间戳头；signature 签名头；input 待验签原文；now 当前 Unix 秒。@return 签名和时间戳是否有效。
	bool verifySignature(std::string_view secret,
						 std::string_view timestamp,
						 std::string_view signature,
						 std::string_view input,
						 std::int64_t now);
} // namespace gamehub
