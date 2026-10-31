/** @file Crypto.cpp
 * @brief 用 OpenSSL 计算摘要和签名，日志中不记录原文或密钥。
 */
#include "Crypto.h"
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
							   std::string_view body)
	{
		return std::string(timestamp) + "\n" + std::string(method) + "\n" + std::string(path) + "\n" + sha256(body);
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
