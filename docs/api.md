# API 与接入协议

公开实现包含演示渠道、统一投递和管理接口；真实渠道的签名、订单来源及游戏经济事务由使用方的宿主实现。构建、迁移和环境变量见[项目说明](../README.md)。

## 请求约定

路径区分大小写。标识使用 ASCII 字母、数字及 `_ - : .`；游戏、渠道命名空间、任务类型最长 32 字节，业务键最长 96 字节。游戏标识不能是独立的 `.` 或 `..`。路径中的百分号编码、加号、反斜杠、重复斜杠和独立点路径段会被拒绝，并关闭连接。不要编码路径标识。

JSON 请求使用 `Content-Type: application/json`。服务请求体默认上限 65536 字节，业务载荷上限 49152 字节；具体字段还有各自限制。除任务列表的筛选参数和由渠道适配器解释的原始回调 target 外，接口不接受查询字符串。内置演示回调不接受查询字符串。

事件 ID、任务 ID、尝试 ID、代次和控制 revision 在响应中使用十进制字符串。区服号、配置版本、尝试次数和状态为 JSON 整数；调用方应使用能精确处理 64 位整数的解析器。配置版本最大为 `9223372036854775807`，区服号为 `1..4294967295`。数据库输出的时间字符串按 UTC 解释，通常为 `YYYY-MM-DD HH:MM:SS.ffffff`；任务筛选时间使用 UTC Unix 秒。

管理接口需要 `Authorization: Bearer <令牌>`。健康和就绪探针不需要令牌，应通过部署网络限制来源。错误通常为 `{"error":"说明"}`；HTTP 框架或前置限流也可能返回纯文本。

| 状态码 | 含义 |
| --- | --- |
| 400 | 字段、路径、查询或请求格式不合法 |
| 401 / 403 | 身份无效 / 角色或游戏范围不足 |
| 404 | 游戏、区服、事件、任务或配置版本不存在 |
| 409 | 同身份内容冲突、控制版本冲突或当前状态不允许 |
| 413 / 429 | 内容过大 / 限流 |
| 503 | 未就绪或依赖暂时不可用，写入结果可能未知 |

限流拒绝会关闭连接。回调或管理写操作断连、超时或返回 503，不等于事务一定失败；保留业务身份并查询结果，不能生成新订单代替重试。

## 签名与管理权限

演示渠道回调、Hub 出向通知及游戏配置请求使用下列 HMAC 协议。正式渠道必须按自己的协议实现 `ChannelAdapter`。

```text
digest = 小写十六进制 SHA256(完整 HTTP 实体字节)
input = timestamp + "\n" + METHOD + "\n" + PATH + "\n" + digest
启用 key-id 时：input = input + "\n" + keyId
signature = 小写十六进制 HMAC-SHA256(secret, input)
```

请求携带 `X-Timestamp`（UTC Unix 秒）、`X-Signature`；启用轮换时再携带 `X-Key-Id`。METHOD 大写，PATH 不含源站或查询字符串。GET 对空 body 计算摘要。签名末尾没有额外换行；无 key-id 时也不追加空行。Content-Length 和 chunked 都按解码后的实体签名，不签分块帧。验签前不得重新序列化 JSON。允许时钟偏差为正负 300 秒。

渠道使用该渠道配置的密钥；Hub 出向和游戏配置拉取、回报使用所属游戏的出向密钥。开启轮换后按 key-id 唯一选择当前或旧验签密钥，未知标识拒绝。无标识请求只接受显式配置的 legacy 密钥；尚未启用轮换的旧配置继续使用原单密钥。配置字段和轮换步骤见项目说明。

管理 JWT 使用 HS256 和独立密钥，标准头部 `kid` 对应管理轮换配置。必须包含非空 `sub`、`role`、`games`、尚未过期的整数 `exp`；`iss`、`aud` 与服务配置一致，有 `nbf` 时也会验证。

```json
{"sub":"synthetic-operator","role":"operator","games":["demo"],"exp":2000000000,"iss":"gamehub-admin","aud":"gamehub"}
```

这只是 claims 形状示例，实际令牌应由签发工具生成短有效期。`viewer` 可读取，`operator` 可读取和写入。`games:["demo"]` 只允许该游戏；全局统计、全局 runtime 和 metrics 要求 `games:["*"]`。有效管理 JWT 不能替代游戏内部接口的 HMAC。

## 渠道回调

`POST /api/callback/{game}/{channel}` 根据配置中的渠道实例查找适配器。适配器读取原始方法、path、target、body 和请求头，完成验签与规范化。Hub 将事件、首次固定的区服任务和审计一起持久提交，成功后才交给该渠道的 `reply` 生成应答。

成功、重复、业务冲突及依赖错误的 HTTP 状态、Content-Type、body 可以由具体渠道定制，不能假定所有渠道都使用下述演示 JSON。适配器应只在持久受理成功后返回渠道成功应答；路由未找到、HTTP 解析或前置限流拒绝发生在渠道适配器之外。

内置 `demo-hmac-v1` 的演示请求为：

```http
POST /api/callback/demo/demo
Content-Type: application/json
X-Timestamp: <当前Unix秒>
X-Signature: <按本次实体计算的签名>

{"bizKey":"synthetic-order-1","channelOrderId":"synthetic-order-1","serverId":1,"playerId":"synthetic-player","productId":"synthetic-product","amountMinor":100,"currency":"CNY"}
```

body 恰好包含这七个字段。`bizKey` 必须等于 `channelOrderId`，区服必须已配置；`amountMinor` 为 `1..1000000000000` 的整数，单位为币种最小单位；`currency` 为三个大写 ASCII 字母。演示协议校验格式，不提供真实商品定价或支付真实性验证。

成功返回 HTTP 200：

```json
{"eventId":"123","accepted":true,"duplicate":false}
```

相同业务内容重试返回原 eventId 和 `duplicate:true`。事件身份是 `(gameId, taskType, namespace, bizKey)`，其中演示 taskType 为 `recharge_notify`，namespace 来自渠道商户配置。对象键顺序和 JSON 空白不改变规范化业务摘要；相同身份修改金额、商品、玩家、区服或载荷版本会返回 409。事件归档后仍保留身份和摘要。

## Hub 到游戏服的投递

Hub 对配置中的目标发送带游戏出向签名的 JSON POST。当前统一封装为：

```json
{
  "taskId":"456",
  "eventId":"123",
  "gameId":"demo",
  "serverId":1,
  "taskType":"recharge_notify",
  "namespace":"demo",
  "bizKey":"synthetic-order-1",
  "schemaVersion":1,
  "payloadVersion":1,
  "fingerprintVersion":1,
  "payload":{"bizKey":"synthetic-order-1","channelOrderId":"synthetic-order-1","serverId":1,"playerId":"synthetic-player","productId":"synthetic-product","amountMinor":100,"currency":"CNY"}
}
```

`schemaVersion` 是封装版本，`payloadVersion` 是业务载荷版本，`fingerprintVersion` 是判重摘要规则版本，当前均为 1。现有任务类型为 `recharge_notify`、`announce`、`config_push`；公告 payload 是提交的公告对象，配置通知 payload 仅为 `{"version":1}`。自定义宿主的载荷规则需要双方明确约定，不能仅靠更换适配器注册名称改变统一通信契约。

通知是至少一次投递，超时、断连、租约回收和人工重排都可能再次送达。游戏服必须验证签名、目标游戏/区服及载荷版本，以 `(gameId, serverId, taskType, namespace, bizKey)` 持久判重，并将判重记录与实际业务效果原子提交。同身份不同内容应拒绝并进入人工核对；不能使用 taskId 或内存缓存代替业务去重。

只有实际业务事务已经提交，或确认同身份之前已经提交，才能返回 HTTP 200：

```json
{"taskId":"456","committed":true}
```

Hub 要求 taskId 是与本次任务完全匹配的字符串，committed 是布尔 true。收到请求、进入内存队列或数据库入队成功均不能据此确认。非 200、错误 taskId、未确认或响应超限都会继续按任务策略处理。确认实体默认上限为 16384 字节，由 `worker.maxResponseBodyBytes` 控制；超限会关闭连接并记录 `response_too_large`。游戏服提交结果未知时必须按原身份恢复，不能盲目再次发奖。

## 公告与配置

以下发布接口需要对应游戏的 operator，成功响应与演示回调的受理响应相同。

```text
POST /admin/announce/demo
{"bizKey":"synthetic-announce-1","serverId":0,"content":"合成公告"}

POST /admin/config/demo/1
{"bizKey":"config:1","serverId":0,"content":{"syntheticSetting":true}}
```

公告只接受这三个字段，content 是非空字符串，最多 8192 字节。serverId=0 广播到首次受理时已配置的全部区服，正数为指定服。后续新增区服不会收到历史广播。

配置也只接受示例三个字段，必须广播，业务键必须为 `config:版本号`。新版本递增，已发布内容不可修改；重试相同旧版本不回退当前 head。回滚配置时，以更高版本重新发布旧内容。

游戏服签名请求 `GET /internal/config/{game}/{server}/{version}`，version=0 取最新，正数取指定版本。响应示例：

```json
{"version":1,"content":{"syntheticSetting":true},"canonicalContent":"{\"syntheticSetting\":true}","sha256":"67da7a0561ea82341e0a62e09a1f6eca18e836c3b94e13e414057aa3b1607e02"}
```

接收端直接计算 `SHA256(canonicalContent 的 UTF-8 字节)` 与 sha256 比较，再解析应用内容，不要对 content 重新序列化后验摘要，尤其不能依赖不同语言的浮点表示一致。校验通过后单调、原子地应用配置并保存版本与摘要；重启需补拉最新，失败可重试，乱序通知不能覆盖已应用的新版本。

实际应用成功后，用同一游戏的出向签名回报：

```text
POST /internal/config-applied/demo/1
{"version":1,"contentHash":"67da7a0561ea82341e0a62e09a1f6eca18e836c3b94e13e414057aa3b1607e02"}
```

body 必须恰好是 version 和 contentHash；摘要为 64 个小写十六进制字符。版本不存在返回 404，摘要不符或旧版本覆盖新状态返回 409。同版同摘要可重复回报。成功响应为：

```json
{"gameId":"demo","serverId":1,"version":1,"contentHash":"67da7a0561ea82341e0a62e09a1f6eca18e836c3b94e13e414057aa3b1607e02","applied":true}
```

`GET /admin/config-status/{game}/{after}` 需要游戏 viewer/operator，按 serverId 升序返回最多 100 条已报告状态，包含 serverId、version、contentHash、reportedAt。未报告的区服不会伪造为已应用。非空页 nextAfter 是最后一个 serverId 数字，空页为字符串 `"0"`。任务 done 表示通知被确认，应结合应用状态判断区服实际版本。

## 查单、尝试历史与筛选

以下读取需要对应游戏的 viewer/operator，不返回事件 payload 或配置正文。

| 路径 | 返回内容 |
| --- | --- |
| GET /admin/events/{game}/{type}/{namespace}/{key} | 完整事件身份及其活动、归档任务 |
| GET /admin/attempts/{game}/{id}/{after} | 指定任务各代次的持久尝试，按尝试 ID 翻页 |
| GET /admin/tasks/{game}/{after} | 活动任务，支持下表筛选 |
| GET /admin/archive/{game}/{after} | 最多 100 条归档 done 任务元信息 |
| GET /admin/audit/{game}/{after} | 最多 100 条审计：id、actor、action、targetId |

事件响应包含 `id, gameId, taskType, namespace, bizKey, requestHash, payloadVersion, fingerprintVersion, createdAt, payloadRedacted, tasks`。任务元信息如下，事件查询的 archived 可以为 true，活动列表为 false：

```json
{"id":"456","eventId":"123","serverId":1,"status":2,"attempts":1,"generation":"1","lastError":"","createdAt":"2026-01-01 00:00:00.000000","updatedAt":"2026-01-01 00:00:01.000000","nextRetryAt":"2026-01-01 00:00:01.000000","leaseUntil":null,"archived":false}
```

状态为 `0=pending, 1=delivering, 2=done, 3=failed`。尝试历史每项包含 `id, generation, attemptNumber, startedAt, finishedAt, outcome, errorCategory`；outcome 为 `in_flight`、`success`、`failure` 或 `lease_expired`，在途 finishedAt 为 null。错误类别为固定分类，不保存原始第三方响应。重排和归档不清除已有尝试；查询只返回实际记录，不推测或补造未记录的尝试。

任务和尝试列表形如 `{"items":[...],"nextAfter":"456"}`，从 after=0 开始，以返回的最后 ID 继续；空页 nextAfter 为 `"0"`，应结束遍历。归档和审计列表仅返回 `{"items":[...]}`，调用方取最后一项 id 继续，到空页结束。归档任务返回 id、eventId、serverId、status=2、attempts、generation。

`GET /admin/tasks/demo/0?status=3&serverId=1&limit=20` 的查询白名单：

| 参数 | 范围与语义 |
| --- | --- |
| status | 整数 0..3，不传则全部活动状态 |
| serverId | 正区服号，不传则全部区服 |
| createdFrom | UTC Unix 秒，包含下界 |
| createdBefore | UTC Unix 秒，不包含上界 |
| limit | 1..100，默认 100 |

时间范围为 `0..253402300799`，同时提供时下界必须小于上界。未知或重复参数返回 400。游标始终是任务 ID，不是时间偏移。

`POST /admin/tasks/{game}/{id}/replay` 需要 operator，body 必须为空，仅对 failed 成功返回 `{"replayed":true}`，其他状态返回 409。重排清零当前重试次数、使旧代次失效，保留同一业务身份并写审计。failed 不能证明游戏没有发奖；先核对权威账本，再明确执行重排，不根据对账差异自动补发。

## 共享控制、归档与监控

`GET /admin/control/{game}` 允许游戏 viewer/operator，响应示例：

```json
{"gameId":"demo","revision":"0","deliveryEnabled":true,"maxConcurrentDeliveries":0,"maxConcurrentPerServer":0,"scope":"startup"}
```

无持久覆盖时 revision 为 `"0"`，值来自启动配置；存在覆盖则 scope 为 `"shared"`。operator 使用版本比较更新：

```text
POST /admin/control/demo
{"expectedRevision":0,"deliveryEnabled":false}
```

expectedRevision 是整数，必须与当前版本一致，此外至少给一个控制字段。两个额度字段都是 0..32；0 表示自动/继承配置语义，实际限额仍受实例的全局 concurrency 约束。成功响应返回递增的字符串 revision 和完整控制值，更新与审计同事务，冲突返回 409。不要在冲突后用旧快照自动覆盖。

控制由各实例每秒刷新，暂停停止新的领取，不撤销已领取任务。额度仍为每进程限额；`GET /admin/runtime/{game}` 的 controlRevision 表示该实例已应用版本，不能当作全系统瞬时一致或分布式总额度。

归档需要游戏 operator：

```text
POST /admin/archive/demo
{"keepDays":30,"batchSize":100,"redactPayload":false}
```

三个字段必须齐全：keepDays 为 1..36500，batchSize 为 1..1000，redactPayload 为布尔值。只归档超过保留期的 done；复制历史、删除活动任务和审计同事务。响应为 `{"archivedTasks":100,"redactedEvents":0}`。

redactPayload=true 会再用独立事务，分批清空已过保留期且全部任务已归档的事件 payload。两个阶段可能分别提交，报错后可再次执行相同维护，不会重复复制历史。事件 ID、身份、摘要、审计与配置版本仍保留，清理后迟到回调仍判重。

| 接口 | 权限与返回 |
| --- | --- |
| GET /admin/health | 无 JWT，`{"status":"ok"}`；只表明进程存活 |
| GET /admin/ready | 无 JWT，`{"ready":true}` 为 200；未就绪为 503 |
| GET /admin/stats/{game} | 游戏 viewer/operator；持久任务计数和积压 |
| GET /admin/stats | 全局 viewer/operator；上述字段及当前进程回调计数 |
| GET /admin/runtime/{game} | 游戏 viewer/operator；本进程该游戏及区服配额、在途与结果 |
| GET /admin/runtime | 全局 viewer/operator；scope=process、总额度与各游戏 |
| GET /admin/metrics | 全局 viewer/operator；Prometheus 文本 |

stats 字段为 pending、delivering、done、failed、archivedDone、completedTotal、oldestPendingSeconds；completedTotal=done+archivedDone，最老 pending 从任务创建时间计，空队列为 0。全局还含 callbacksAccepted、callbacksDuplicate、requestsRejected。runtime 及这些进程内计数重启清零；持久计数跨实例共享。

Prometheus 的 `gamehub_tasks`、`gamehub_game_tasks`、归档和最老积压来自共享数据库，同库多实例采集应按范围取 max，不能把副本相加。`gamehub_delivery_*`、`gamehub_target_*`、回调和数据库计数属于当前进程；对这些 counter 使用 rate/increase。claimed、failures 为独立总数，results_total 的 result 分类互斥，不能再把总数与分类相加。耗时使用 seconds 单位的 histogram，包含回调、投递、完成、数据库查询及连接获取。指标标签只含配置游戏、区服、状态和固定结果类别，不含订单或玩家身份。
