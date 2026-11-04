/** @file DeliveryProtocol.h
 * @brief 游戏接入协议编码，与数据库记录映射分离。
 */
#pragma once
#include "Domain.h"

namespace gamehub
{
	inline constexpr unsigned kDeliverySchemaVersion = 1;

	/// @param task 已领取且自持有数据的业务任务，payload 是原始业务 JSON。
	/// @return 统一投递协议 JSON；版本字段只描述契约，不参与业务幂等键或摘要。
	std::string encodeDelivery(const DeliveryTask& task);
} // namespace gamehub
