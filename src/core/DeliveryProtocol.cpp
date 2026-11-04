/** @file DeliveryProtocol.cpp
 * @brief 保持既有投递字段，附加显式契约版本。
 */
#include "DeliveryProtocol.h"

namespace gamehub
{
	std::string encodeDelivery(const DeliveryTask& task)
	{
		auto envelope = boost::json::object {{"taskId", std::to_string(task.id)},
											 {"eventId", std::to_string(task.eventId)},
											 {"gameId", task.gameId},
											 {"serverId", task.serverId},
											 {"taskType", task.taskType},
											 {"namespace", task.nameSpace},
											 {"bizKey", task.bizKey},
											 {"payload", boost::json::parse(task.payload)},
											 {"schemaVersion", kDeliverySchemaVersion},
											 {"payloadVersion", task.payloadVersion},
											 {"fingerprintVersion", task.fingerprintVersion}};
		return boost::json::serialize(envelope);
	}
} // namespace gamehub
