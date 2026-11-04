/** @file IngressLimits.h
 * @brief 按配置游戏隔离回调配额，管理与内部接口使用独立配额。
 */
#pragma once
#include "core/Config.h"
#include <core/Middleware.h>

namespace gamehub
{
	/// @param config 启动后固定的游戏配额配置。@return 路由前执行的校验与限流；拒绝未读完的请求时关闭连接。
	hical::SyncBeforeHandler makeIngressLimits(const Config& config);
} // namespace gamehub
