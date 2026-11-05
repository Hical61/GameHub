/** @file Crypto.cpp
 * @brief 用 OpenSSL 计算摘要和签名，日志中不记录原文或密钥。
 */
#include "Crypto.h"
#include "Config.h"
#include "Domain.h"
#include <array>
#include <charconv>
#include <limits>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <stdexcept>

namespace gamehub
{
	namespace
	{
		constexpr std::size_t kMaxJwtBytes = 4096;
		constexpr std::size_t kMaxJwtHeaderBytes = 1024;

		std::string hex(const unsigned char* bytes, unsigned size)
		{
			constexpr char kDigits[] = "0123456789abcdef";
			std::string result(size * 2, '0');
			for (unsigned i = 0; i < size; ++i)
			{
				result[i * 2] = kDigits[bytes[i] >> 4];
				result[i * 2 + 1] = kDigits[bytes[i] & 15];
			}
			return result;
		}

		std::string decodeJwtHeader(std::string_view encoded)
		{
			constexpr std::string_view kAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
			if (encoded.empty() || encoded.size() > kMaxJwtHeaderBytes || encoded.size() % 4 == 1)
			{
				throw ApiError(401, "管理令牌头格式无效");
			}
			std::string decoded;
			decoded.reserve(encoded.size() * 3 / 4);
			std::uint32_t buffer = 0;
			unsigned bits = 0;
			for (const char character : encoded)
			{
				const auto value = kAlphabet.find(character);
				if (value == std::string_view::npos)
				{
					throw ApiError(401, "管理令牌头格式无效");
				}
				buffer = (buffer << 6) | static_cast<std::uint32_t>(value);
				bits += 6;
				if (bits >= 8)
				{
					bits -= 8;
					decoded.push_back(static_cast<char>((buffer >> bits) & 0xff));
				}
			}
			if (bits && (buffer & ((1u << bits) - 1)) != 0)
			{
				throw ApiError(401, "管理令牌头编码不规范");
			}
			return decoded;
		}
	} // namespace

	std::string sha256(std::string_view data)
	{
		std::array<unsigned char, EVP_MAX_MD_SIZE> digest {};
		unsigned size = 0;
		if (EVP_Digest(data.data(), data.size(), digest.data(), &size, EVP_sha256(), nullptr) != 1)
		{
			throw std::runtime_error("SHA-256 计算失败");
		}
		return hex(digest.data(), size);
	}

	std::string hmacSha256(std::string_view secret, std::string_view data)
	{
		std::array<unsigned char, EVP_MAX_MD_SIZE> digest {};
		unsigned size = 0;
		if (secret.size() > static_cast<std::size_t>(std::numeric_limits<int>::max())
			|| !HMAC(EVP_sha256(),
					 secret.data(),
					 static_cast<int>(secret.size()),
					 reinterpret_cast<const unsigned char*>(data.data()),
					 data.size(),
					 digest.data(),
					 &size))
		{
			throw std::runtime_error("HMAC 计算失败");
		}
		return hex(digest.data(), size);
	}

	bool secureEqual(std::string_view left, std::string_view right)
	{
		return left.size() == right.size() && CRYPTO_memcmp(left.data(), right.data(), left.size()) == 0;
	}

	std::string signatureInput(std::string_view timestamp,
							   std::string_view method,
							   std::string_view path,
							   std::string_view body,
							   std::string_view keyId)
	{
		auto input =
			std::string(timestamp) + "\n" + std::string(method) + "\n" + std::string(path) + "\n" + sha256(body);
		if (!keyId.empty())
		{
			input += "\n";
			input += keyId;
		}
		return input;
	}

	std::string readJwtKeyId(std::string_view token)
	{
		const auto first = token.find('.');
		const auto second = first == std::string_view::npos ? first : token.find('.', first + 1);
		if (token.size() > kMaxJwtBytes || first == std::string_view::npos || second == std::string_view::npos
			|| first == 0 || second == first + 1 || second + 1 == token.size()
			|| token.find('.', second + 1) != std::string_view::npos)
		{
			throw ApiError(401, "管理令牌格式无效");
		}
		boost::system::error_code error;
		const auto header = boost::json::parse(decodeJwtHeader(token.substr(0, first)), error);
		if (error || !header.is_object())
		{
			throw ApiError(401, "管理令牌头格式无效");
		}
		const auto* keyId = header.as_object().if_contains("kid");
		if (!keyId)
		{
			return {};
		}
		if (!keyId->is_string() || !validKey(keyId->as_string(), kMaxKeyIdLength) || keyId->as_string() == "."
			|| keyId->as_string() == "..")
		{
			throw ApiError(401, "管理令牌密钥标识无效");
		}
		return std::string(keyId->as_string());
	}

	bool verifySignature(std::string_view secret,
						 std::string_view timestamp,
						 std::string_view signature,
						 std::string_view input,
						 std::int64_t now)
	{
		if (timestamp.empty() || timestamp.size() > 12)
		{
			return false;
		}
		std::int64_t seconds = 0;
		auto [end, error] = std::from_chars(timestamp.data(), timestamp.data() + timestamp.size(), seconds);
		if (error != std::errc {} || end != timestamp.data() + timestamp.size() || seconds < 0
			|| seconds < now - kSignatureWindowSeconds || seconds > now + kSignatureWindowSeconds)
		{
			return false;
		}
		return signature.size() == 64 && secureEqual(hmacSha256(secret, input), signature);
	}
} // namespace gamehub
