# 渲染交付恢复协议 v2

## 范围

针对压测第411条：节点已成片，网络拒绝使上传/上报失败，旧Relay在40分钟后跨节点重派，实际重复生成。
本改动仅涉及Relay私有库及轮询器，不改主站任务/点数表、模板、渲染器、并发或素材选择。

## 协议和恢复

- 新poller以delivery_protocol=2领取，Relay同事务持久化job/node/claim_token。
- 私有表delivery_claims仅新增，不修改旧jobs列。新任务不会按超时自动跨节点重派；旧协议维持原行为以支持滚动升级，发布必须先确认旧在途为零。
- /v1/recover要求节点凭据，仅返回指定节点的未完成领取或已收文件未确认metadata的任务。领取凭据随原归属恢复，不产生新执行代次。
- 上传、状态确认、结果上报均校验node+claim_token；当前部署仍使用既有可信节点共享NODE_TOKEN体系，claim_token不替代节点身份认证。
- 节点本地私有outbox按原Relay ID记录原payload、已准备payload、本地job_id、rendered/ready/complete状态、输出SHA以及phase_times。每次转换原子写入并fsync。
- 原节点重启从outbox及Relay恢复已有领取。已知local_id只查原任务；本地提交确认丢失时，同一冻结payload与相同X-Request-Id重放，依赖当前节点API的持久幂等create，不改变payload或换节点。
- 上传前私有spool保存MP4与SHA。网络故障保留ready，不上报渲染失败、不重生成。重试前查询Relay完成状态，已收文件则只确认metadata。
- Relay对单任务串行处理上传/报告；完整长度和SHA验证后原子发布文件。已完成重复上传必须相同SHA，返回既有结果，不再上传COS；不同SHA拒绝。
- 完成metadata确认不重写完成时间，迟到失败上报不能降级completed。原claimed_at不因新协议恢复而覆盖。
- outbox单进程锁及线程预留防并发重复处理；未确认领取计入每节点槽位上限，防网络故障期间无限继续领取。

## 运行与发布

先停止空闲poller接单，再升级Relay并验证新私有表/health，再启动升级后的poller。
Linux NODE_STATE_DIR使用私有用户目录；Windows显式使用D盘管理员私有目录，不能把状态目录放进会清理的代码release。
只需重启Relay及四台poller，不重启renderer/HY/主站。回滚代码前必须确认无v2在途，不能让旧poller处理v2领取；严禁恢复旧DB快照覆盖任务/点数。
状态目录含内部领取凭据和任务输入，不提交、不打印，备份限制访问。确认完成后删除本轮询器spool副本，节点原成片和Relay文件不受影响。

## 验证与边界

测试覆盖真实HTTP链：本地只提交一次→Relay已存成片但模拟确认丢失→重建DeliveryStore→只补metadata，COS只写一次。
同时覆盖故障重启、未知完成状态、并发重复上传、SHA冲突、错误node/token、迟到失败、每节点5槽、进程锁与旧协议回归。

这不是永久离线时的自动容灾。原节点/本地DB/outbox永久丢失时需人工核验，不能凭超时推断未生成；health暴露delivery_recovery_required。
主站20分钟总任务预算不在此改动中，长时断网仍可能导致主站任务结束，需要另行对齐产品生命周期。
COS失败时沿用已有Relay本地成片交付合同，cos_uploaded=false可观测；不将其描述为云端副本必达。
进程在COS成功与SQLite完成提交之间崩溃时，可重复写同一COS key的相同内容；不保证物理PUT恰好一次，但不会重新渲染或更换结果。
本阶段不解决win3060浏览器/媒体probe故障，也不宣称1000条压测通过。
