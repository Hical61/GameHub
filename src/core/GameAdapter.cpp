/** @file GameAdapter.cpp
 * @brief 注册游戏适配器，注册表只依赖统一接口。
 */
#include "GameAdapter.h"

namespace gamehub
{
	void GameRegistry::add(std::unique_ptr<GameAdapter> adapter)
	{
		if (!adapter || !validKey(adapter->gameId(), 32))
		{
			throw std::invalid_argument("适配器游戏标识无效");
		}
		const auto id = std::string(adapter->gameId());
		if (!adapters_.emplace(id, std::move(adapter)).second)
		{
			throw std::invalid_argument("适配器游戏标识重复");
		}
	}

	const GameAdapter& GameRegistry::find(const std::string& id) const
	{
		auto found = adapters_.find(id);
		if (found == adapters_.end())
		{
			throw ApiError(404, "游戏适配器不存在");
		}
		return *found->second;
	}
} // namespace gamehub
