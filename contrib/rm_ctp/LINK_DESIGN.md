# RM+CTP 建链：源码核验与最小设计初稿

日期：2026-10-23。对应 REQUIREMENTS.md 的 A4，并涉及 A5/A6/A7 与 C1。
状态：已完成源码核验、第10节的软件实施选择和模型验证；完整C路径及节点证据尚未完成。
第10节作为后续实现基线，实际CTP必要保证和验收仍按依赖闭合，A4、C1继续保持□。
完整 C1 不作为所有建链工作的统一前置；各项保证按实际依赖在第 7 节分级。

## 1. 本轮边界与固定版本

| 代码 | 固定版本 | 证据边界 |
| --- | --- | --- |
| 当前 HUCX | 2e43d42a68c19dde9a62ee7a486ccd9699acb5db | 产品与已测 1f81b035 相同；本轮只增加文档 |
| HMPI 参考源码 | 52d943345a85efc10de2d59311eb4bd4fa94444e | 可确认此源码的调用链，尚未证明节点 mpi_run_15c7293 构建与其完全相同 |
| UMDK 接口及公开实现 | 50288b2a4a62fb76e3584acd8d8ed1c1071a485d | 三个公开头文件此前与节点匹配；不能据此认定 provider/驱动二进制相同 |
| brpc 参考 | e6fd4c5a803bfe4c803456081f064632cc883539 | 只取工程设计参考，环境及协议边界见既有研究记录 |

2026-10-23 旧 d58 安装的 RC/RM 8B 控制均退出 1，日志位于实验根下
old8b_recheck_20261023_9h4hc221。尚未读取失败日志，不能断言本轮仍是先前的
UM 导入/控制队列超时，更不能确认 UBMEM 切换。暂停新增硬件结果，继续源码工作。

## 2. 已从源码闭合的地址发布链

HMPI 的 ompi/mca/pml/ucx/pml_ucx.c：

1. mca_pml_ucx_init 创建 UCP worker，随后调用 send_worker_address。
2. 不支持 ADDRESS_FLAGS 时，worker_get_address 后通过 PMIx modex 发布全局地址。
3. 支持该编译分支时，分别发布远端网络地址与本地地址；不能假设节点选择了哪条分支。
4. add_proc_common 从 modex 取得对端地址，然后以 REMOTE_ADDRESS 调用 ucp_ep_create。
5. add_procs 会遍历传入的进程列表创建 UCP EP；get_ep 另有缺失时按需创建的兜底。

因此，不能一概描述为“所有连接都等到第一次 MPI_Send 才创建”。地址何时对其他
进程可获得、哪些 EP 在初始化就已创建，要结合实际构建/启动路径验证。

HUCX 路径：

worker_get_address / worker_query
→ worker_address_pack
→ address_pack
→ uct_iface_get_address
→ worker 地址进入 HMPI/PMIx
→ 对端 ucp_ep_create 解包
→ wireup_connect_lane_to_iface
→ uct_ep_create 收到 DEV_ADDR / IFACE_ADDR。

默认 worker 地址打包标志已经包含 device 和 iface 地址。address_pack 的 iface
地址分支依赖打包标志与 iface_addr_len，不需要在 MPI 中新增 Jetty 专有消息。
当前 RM 回调为空且长度未提供，不能利用这条路径传递共享 Jetty 的稳定地址。

**决策：首个候选仅修改 RM transport；HMPI、通用 UCP 地址格式及 RC/UM 不预设修改。**
若实际版本表现不同，先给出差异证据，再扩大范围。

## 3. RC、UM、当前 RM 的精确区别

| 项目 | RC | UM | 当前 RM |
| --- | --- | --- | --- |
| 本地 Jetty | EP 构造时创建 | iface 构造时创建 | iface 构造时创建，EP 借用 |
| 接收 JFR | 已使用共享 JFR | iface 管理 | iface 管理，不是本轮新增 SRQ |
| 稳定 iface 地址 | 本轮不改 | get_address 提供 Jetty ID/模式 | 尚未实现 |
| 目标信息 | EP 地址交换并导入/绑定 | dev/iface 地址导入，并有目标表 | EP 地址交换，逐 EP 导入 CTP 目标 |
| 逻辑 EP | 成对连接路径 | 仍有 CREQ/CREP 交换逻辑身份 | 数据头携带 dep_id，FC 通过其找接收端 EP |

UM 的 create_connected_common 并非拿到 iface 地址就结束：它仍准备并发送/排队
CREQ，随后 CREP 设置对端 EP ID。我们借鉴其稳定地址发布，不复制整套 UM 控制/
PSN/重传机制，也不把它描述为首次通信零握手的既有实现。

## 4. 关键发现：普通 AM 与流控返回应分开

base/urma_iface.c 的 handle_am：没有 FC 标志时，直接按 AM ID 调用上层回调；
只有带 FC 标志时才进入 RM fc_handler。普通 AM 接收本身不靠 dep_id 查找 EP。

RM fc_handler 则通过 hdr.ep_id 查 iface->eps，读取该 EP 的窗口/状态，并从
对应 EP 的目标发送 grant。因此，单纯新增 iface 地址或置 CONNECT_TO_IFACE
不能解决没有成对接收 EP 时的回信问题。

**最小方案方向：本地EP保留完成、pending和flush；新路径的额度及FC请求由peer共用。
接收侧不为普通AM强制创建成对业务EP，FC通过明确回信信息和一次性请求标识返回。**
这使硬件目标寻址与本地软件操作身份分离，避免为取得对端 EP ID 再复制 CREQ/CREP。
该方向是设计候选，不是已实现/已验收能力。

UCX 已有 DC 参考：dc_mlx5 的独立 FC 请求携带回信信息与序号，grant 在本地
表中校验请求是否仍有效，避免已销毁 EP 或旧请求的迟到回复被误用。这里只借鉴
所有权和校验方法，不引入 mlx5 队列/报文，也不复制其重传定时器。

## 5. 地址、请求与资源归属

| 信息/资源 | 建议归属与发布时机 | 约束 |
| --- | --- | --- |
| EID、uasid 等 device 信息 | 现有 UCT device 地址 | 不在 MPI 重复定义格式 |
| Jetty ID、RM 模式、地址版本 | RM iface 地址，iface 完成资源初始化后发布 | 不含每 EP 的 ID、pi/ci 或进程内指针 |
| 当前 KA segment 元数据 | 首版保留现有稳定字段于 iface 地址 | 当前连接实际会 import_seg；不因能力被屏蔽就顺便删除其副作用 |
| 本地 EP ID、pi/ci、outstanding | 本地 EP | 保留现有共享 Jetty 完成归属，不发送本地指针给对端解引用 |
| 导入的目标与相应 segment | iface 内按完整远端地址管理的 peer 记录 | 发布成功前完成初始化；失败不留下可命中的半初始化表项 |
| FC 请求标识 | 发送iface分配，每次请求唯一；本地表关联peer | grant先查表校验；peer失效撤销请求，单个EP销毁只移除其等待关系 |
| FC 回信地址 | 请求携带完整可导入的 dev/iface 信息 | 不假设接收方已主动创建反向业务 EP |
| 非 inline 控制缓冲与目标引用 | 本轮 WR/待发请求持有至完成或明确取消 | 不提前复用，不随单个业务 EP 销毁而释放其他请求依赖的目标 |

目标记录的键至少覆盖 EID、uasid、Jetty ID、模式/CTP、影响导入语义的配置及地址
版本；不直接对含未初始化 padding 的结构做 memcmp。对固定 token/order 配置
可由 iface 统一约束，但必须说明，不能只按 Jetty ID 缓存。

首版建议 peer 记录由 iface 保留至清理，消除反复 EP 创建/销毁带来的重复 import；
销毁 EP 只撤销本地引用和 pending，不立即释放共享目标。代价是缓存随访问过的远端
iface 数量增长，应记录这一点；不宣称总资源恒定。未来确需淘汰再增加独立生命周期设计。

普通 UDMA 公开 import_jetty_ex 每次 calloc 软件目标对象，核心库只负责 context
引用及转发，未在这条源码路径发现按远端地址复用软件句柄的表。此发现支持软件目标
复用的必要性，但不等于硬件 TP 未复用，更不是当前 provider 二进制的等价证明。

## 6. 首次通信与 FC 的候选时序

| 阶段 | 候选行为 | 可以宣称的目标/不能宣称的结论 |
| --- | --- | --- |
| 初始化 | iface 创建共享 Jetty/JFR 并准备接收；worker 地址带稳定 iface 字段发布 | 将硬件目标地址纳入现有发布机制；不声称所有地址已主动送到每个进程 |
| 创建发送 EP | 用已知 dev/iface 地址取得 peer 记录并设置本地 EP | 不为查询 Jetty 地址发 RM 专用 CREQ/CREP；导入仍可能有系统/设备控制开销 |
| 首次普通 AM | 通过共享 Jetty 发往远端 iface，上层按 AM ID 收取 | 不要求先建立成对 UCT 接收 EP；UCP 自己的消息/协议握手须独立统计 |
| 窗口接近耗尽 | 独立 FC 请求带请求标识、完整回信地址 | 是按流控需要出现的控制消息，不是逐消息交付 ACK |
| 接收请求 | 按回信地址准备返回目标，使用 iface 控制发送状态发 grant | 目标导入、pending、控制缓冲及完成均有明确归属 |
| 发送方收 grant | 校验标识仍绑定活动peer，按本次申请量恢复一次共享额度并调度pending | 校验iface实例，拒绝迟到/重复grant，不因EP重建重置额度 |
| 关闭/错误 | 先撤销请求映射，取消 pending；按现有完成/延迟回收规则清理 | 不提前释放 DMA 缓冲、共享目标或公共 Jetty |

FC 请求与 grant 使用明确区分的纯控制消息，不复用“普通业务数据头中的远端 EP ID”
作为隐含回信依据。具体序列化字段宽度、版本与控制额度尚待定案；设计可继续推进，
但启用新的 FC 路径前，必须闭合其实际依赖的接收耗尽、控制余量及控制消息保证。
不把完整 C1 设为稳定地址发布的前置，也不把本初稿当作可直接部署的协议。

成功接收 CR 的 remote_id 是潜在优化，但首版不依赖它拼出完整回信地址：公开
UDMA poll 路径可见填写 id/EID，未从所读代码确认 uasid 的填写。完整软件回信
字段可消除这项假设；如以后想省报文，再单独验证 metadata。

## 7. 按实际依赖闭合底层保证

| 问题 | 影响的工作 | 处理时机 |
| --- | --- | --- |
| 成功 CQE 后何时可复用本地 DMA 缓冲 | A 中的发送完成与生命周期 | 保留既有已测完成路径；新增缓冲/引用路径启用前核对，不由远端交付含义替代本地所有权 |
| 接收 WQE 耗尽时等待/重试、终态报错还是可能丢失 | A6 新 FC 路径与 A8 多发送方规模验证 | 对应路径启用或规模验收前明确；不阻塞稳定 iface 地址发布设计 |
| 各 EP 初始窗口总预算、控制消息是否消耗同一接收 WQE、SQ/JFC/RX 余量 | A6 流控和共享资源推进 | 在 FC 控制额度及共享接收预算定案时闭合，不能仅检查单个 EP 的窗口 |
| 成功 CQE 是否意味着远端收到；丢失、顺序与重复如何处理 | 主要属于 C1，决定软件确认/重传的必要范围 | 与 A 并行研究；若 A6 具体控制状态依赖某项保证，只前置该项，不要求先完成全部 C1 |

默认 RM order 在公开核心库被转为 OI，只证明接口配置，不能据枚举名称补齐实际
CTP 顺序/重试/去重契约。保留旧完成路径也不等于已证明全部底层语义；最终候选
仍须满足 UCT AM 对正确性的要求，已有 8B 通过不能代替契约依据。

例如 RX=64 时，四个发送关系各窗口8与二十个发送关系各窗口8的总预算不同。
已有 FC 小窗口通过不能证明未知发送方数量下不会耗尽。若底层不能覆盖耗尽，
必须设计共享接收额度的分配/获取，可能引入首次信用控制；不能同时保证任意规模、
有限接收资源和首次绝无控制交换而不给出依据。

先推进稳定地址发布、既有地址交换接入及目标管理设计；需要外部确认时，带具体
场景和软件选择，优先查对应实现/文档，再请维护方补齐缺口。不让导师先回答完整
C1 才继续 A，也不要求用户重查 priority/TP/RPM。节点实际 HMPI 构建身份在环境
恢复、需要解释运行差异时一次确认，不因源码设计先扩展成全面环境取证。

## 8. 预计最小修改与验证门槛

| 位置 | 计划修改 | 本轮是否实施 |
| --- | --- | --- |
| rm_urma_iface.h/c | 稳定地址、get_address/query、peer/控制状态的所有权 | 否 |
| rm_urma_ep.h/c | 已知 iface 地址创建 EP，保留完成归属；去除 FC 对成对 EP 的依赖 | 否 |
| 既有 test_shared_tx.c/相关验收工具 | 增加地址、无反向业务 EP 的 FC、迟到 grant、失败回滚和引用测试 | 否 |
| HMPI、UCP、公共 URMA、RC/UM | 首版不预设修改；发现确实缺口再单独说明 | 否 |

先完成 A3 已验收修正的正式 AtomGit 产品交付，再开始 A5/A6 产品改动。
A5 地址发布和 A6 逻辑/FC 返回相互依赖；可以保留过程提交，但不能只加能力位
就部署并宣称已完成建链优化。按一个可完整验收的需求集中交付，避免逐函数拆轮次。

验证顺序：

1. 本地检查地址字段/版本、发布不含 EP 私有状态、失败不留脏缓存；保留既有 CPU 回归。
2. 定向测试单向首次发送、接收方没有反向业务 EP 时 FC 可推进、并发/重复创建、
   cancel/destroy 后迟到 grant、目标导入失败、非 inline 控制缓冲和共享完成。
3. 环境恢复后先旧8B控制，再隔离候选加载/8B/1、2、4 peers 和 FC窗口回归。
4. 记录 worker 发布、地址获取、导入、UCP wireup、RM 专用控制消息和首次数据时机。
   地址/控制交换次数分层计数，不把 provider 调用次数当网络报文数。
5. A7通过后逐级到两节点PPN20比较初始化、首次、稳定性能及队列/内存；RM扩展随后进行。

## 9. 参考取舍与源码索引

- [HMPI地址发布/获取与EP创建](https://github.com/kunpengcompute/hmpi/blob/52d943345a85efc10de2d59311eb4bd4fa94444e/ompi/mca/pml/ucx/pml_ucx.c)：复用现有路径；不另写MPI感知Jetty的协议。
- [HUCX地址打包](https://github.com/Kring-lll/hucx/blob/2e43d42a68c19dde9a62ee7a486ccd9699acb5db/src/ucp/wireup/address.c)与[wireup](https://github.com/Kring-lll/hucx/blob/2e43d42a68c19dde9a62ee7a486ccd9699acb5db/src/ucp/wireup/wireup.c)：使用现有iface地址入口；不改通用格式。
- [UM建链](https://github.com/Kring-lll/hucx/blob/2e43d42a68c19dde9a62ee7a486ccd9699acb5db/src/uct/urma/um_urma/um_urma_ep.c)：借鉴资源归属与目标复用，保留对CREQ/CREP事实的说明；不复制其重传栈。
- [当前RM](https://github.com/Kring-lll/hucx/blob/2e43d42a68c19dde9a62ee7a486ccd9699acb5db/src/uct/urma/rm_urma/rm_urma_ep.c)：保留已验收共享发送、完成/pending/flush规则；聚焦新的地址与FC身份处理。
- [公共AM接收](https://github.com/Kring-lll/hucx/blob/2e43d42a68c19dde9a62ee7a486ccd9699acb5db/src/uct/urma/base/urma_iface.c)：普通AM回调与FC查EP分开；不把所有接收都描述为依赖远端EP。
- [UCX DC流控](https://github.com/Kring-lll/hucx/blob/2e43d42a68c19dde9a62ee7a486ccd9699acb5db/src/uct/ib/dc/dc_mlx5.c)：借鉴回信信息、序号与活动请求校验；不移植mlx5发送代码或擅加第二套重传。
- [URMA核心导入与默认order](https://github.com/openeuler-mirror/umdk/blob/50288b2a4a62fb76e3584acd8d8ed1c1071a485d/src/urma/lib/urma/core/urma_cp_api.c)及[UDMA目标导入](https://github.com/openeuler-mirror/umdk/blob/50288b2a4a62fb76e3584acd8d8ed1c1071a485d/src/urma/hw/udma/udma_u_jetty.c)：区分软件句柄、底层TP和实际契约。
- [brpc实现](https://github.com/apache/brpc/blob/e6fd4c5a803bfe4c803456081f064632cc883539/src/brpc/urma/urma_endpoint.cpp)：借鉴就绪阶段、双窗口、控制余量、注册缓冲生命周期；不引入其TCP握手、按连接Jetty池或IOBuf注册池，不将credit ACK当重传ACK。

前述章节记录源码核验与初稿背景；后续软件选择及模型结果见第10节。没有新增节点PASS或性能结论。

## 10. A4软件方案V1：按此实现，不等待导师逐项定方案

2026-10-23后续，用户授权先做可验证实现，再带结果向祝博汇报。以下是本项目的
具体软件选择；模型检查已执行，尚未实现完整C数据/控制路径或完成节点验收。
之前准备的A5地址发布补丁仍是本地候选，未获用户应用/部署证据。

### 10.1 五项选择与一个所有权修正

| 问题 | 实现选择 | 对现有机制的复用 |
| --- | --- | --- |
| 接收侧没有反向业务EP怎么回信 | 请求携带EID、uasid和稳定iface地址；iface内一个控制发送状态按目标发grant | 使用同一个本地Jetty，复用现有WR完成归属、mpool和pending；不按请求/对端新建Jetty |
| 迟到/重复回复如何处理 | iface实例标识加64位单调请求号；回复只查活动请求，校验本次数量，重复/失效回复忽略 | 参考DC的独立FC请求和请求表，沿用本地错误/取消处理 |
| 目标谁持有、何时释放 | iface持有peer记录，按完整远端身份复用，保留至iface关闭；先停硬件再清理 | 普通UDMA导入仍可能分配软件对象；不把缓存等同硬件TP不复用 |
| 多个发送方如何共享接收队列 | receiver维护总预算；一个source iface面向同一destination iface只持一份发送额度，所有逻辑EP共享 | 沿用软件FC和队列机制，改变共享条件下的账户归属，不重复实现数据ACK/重传 |
| 如何证明建链收益 | 分开记录地址获取、目标导入、RM控制和UCP wireup；同时测初始化、首次和稳定通信 | 复用已知RC/RM启动配置，诊断与性能分轮，保留RM改造前基线 |

**修正初稿中“请求号绑定EP、每EP保留独立窗口”的候选：新路径中额度属于peer，
请求表也绑定peer。** 否则同一对端的两个逻辑EP各取得完整初始窗口，会重复放大额度。
EP仍保留自己的pending、完成序号和flush；销毁一个EP只移除它的等待关系，不把
整个peer的额度重新初始化，也不删除其他EP依赖的目标。旧连接模式按既有实现保留。

### 10.2 控制状态、目标与线格式

控制发送状态首版可复用一个iface所有的内部逻辑EP作为完成归属，借用共享Jetty，
并非接收侧逐连接创建反向业务EP。控制WR的target由peer指定；非inline报文各用
自己的注册描述符直到CQE。内部控制EP不向上层暴露，其错误通过共享iface故障通知
业务EP，不能将它当成UCP拥有的业务EP调用上层错误处理。共享SQ/CQ空位不足时将
控制请求排队，TX进展先处理控制，再调度普通pending，避免额度消息长期饥饿。

peer键包括远端EID/uasid/Jetty ID、iface实例标识、地址版本、注册段身份，以及影响
导入的mode/CTP/order/token。初始化完整后才插入缓存；导入中途失败撤销已创建对象。
这里的缓存保存到iface关闭，会随访问过的远端iface数增长，不能宣称总内存恒定。
关闭时先撤销活动请求、取消pending，等待/停止本地DMA，再释放控制描述符和导入目标。

稳定地址下一版拟为48字节：原A5候选32字节字段，加rx_depth(uint32)、
max_sources(uint16)、initial_credit(uint16)及iface_instance(uint64)，版本改为2。
实例标识复用ucs_generate_uuid等既有工具产生，生命周期与iface相同；避免本地请求号
重新计数或远端Jetty ID复用时，将旧控制回复认成新请求。原A5 v1仅验证地址发布基础，
不能直接当作此v2协议已实现/已验证，后续生成新的合并候选并保留原补丁历史。

控制报文序列化包含：控制版本、类型、长度、request_id、请求者实例、目标实例、
credits和保留字段。请求另带完整可导入的回信身份；回复携带相同请求号与本次数量。
接收时先核对长度/版本/实例/模式，按字节复制读取非对齐字段，不发送软件对象指针。
普通AM继续使用既有分派；接收控制消息不要求用对端业务EP编号查本地eps表。

peer本地剩余额度到hard threshold时，可提前排队一次请求，携带“自上一请求以来
新消耗的额度n”；在该回复到来前不再发第二个请求。回复有效时加回n，不能直接将
窗口重置为满额，因为等待回复期间可能又发送了数据。发送post失败不扣数据额度；
控制post临时失败保留同一请求号和同一报文，终态错误进入既有共享iface失败路径。
请求号0不用，64位计数耗尽时报错而不复用。此处不增加网络丢失重发定时器。

只在控制WR尚未发出时可撤销本地登记并重新申请；已经发出的请求遇终态错误，按共享
iface失败处理，不在原peer上悄悄重新赠送初始额度。已成功发出的grant所占预算，也不能
仅因本地取消/错误就认定远端没有收到并加回；其回收依赖实际数据补回或整个实例关闭。
模型中的取消用例验证身份隔离，不代表已经实现网络故障后的额度回收协议。

### 10.3 有界首版的总接收预算

首版无额外初始信用握手仅用于**已明确最多K个source RM iface**的配置；每个source
面向同一destination的全部逻辑EP共享一份额度。K不是EP数量，也不总是每节点进程数：
两节点各20rank、每rank一个选定RM iface，若允许全体互通，上限取39；若只做固定
跨节点星形拓扑可另取经核实的更小界。每进程多iface需要重新计数。未知/超过此界的
场景不自动套用初始额度，不能靠在第K+1个控制请求到达后报错来撤销它已发送的数据。

首版新建链模式要求显式启用共享FC并配置此上界，使用实际可投递深度核算；条件不满足
则不启用该模式，不能沿用旧模式FC=n的配置却仍宣称共享接收预算受到保护。

设实际已准备的接收深度R、每source初始额度W，先留C=2K+2个控制位置，必须满足：

    K * W + C <= R

这里C是本方案在“一peer最多一个活动请求、控制接收位置补回后再发新控制”条件下
选取的保守预算，不是URMA通用常量。须分别核对实际JFR配置深度与成功投递数量，
失败/部分投递或pool不足不能仍发布完整R。发送方从远端地址取W，不从本机配置猜。

| 有界模型配置 | 控制预算C | 初始数据额度K*W | R | 结论 |
| --- | ---: | ---: | ---: | --- |
| K=4,W=8 | 10 | 32 | 64 | 模型预算满足 |
| K=20,W=8 | 42 | 160 | 64 | 必须拒绝 |
| K=39,W=8 | 80 | 312 | 512 | 模型预算满足，未测40rank |
| K=383,W=8 | 768 | 3064 | 4096 | 模型预算满足，未测384rank |

receiver总账B初始为R-C-KW。每实际补回一个数据接收位置，B加1；每成功授予n份额度，
B减n并记为已发放义务。始终保持“未发放预算+已发放且尚未由实际补回结清的额度=R-C”。
已发放义务包括发送方尚未用掉的额度、在途数据，以及已经到达但接收位置尚未补回的数据。
因此可在全局预算内先回复某peer，无须依赖它的请求一定排在自己的所有数据之后到达。

**收到数据不等于位置已经补回。** AM上层可能持有接收描述符，池也可能暂时没有空闲。
只按实际成功补充的数量归还预算；混合接收描述符优先核销控制位置的欠账，其余才记数据。
控制请求/回复的接收位置补回之前，不发下一轮控制消息，避免重复周转逐步吃光控制保留。
grant已授出的额度在重复请求时不再授出；未发出的grant遇临时post失败回滚预算并保留pending。

源码接入点：base/urma_iface.c的recv仅在post_jfr_wr成功后扣减jfr_que_available；
handle_am在回调返回INPROGRESS时将描述符交上层持有。RM需要看到接收完成的分类和
实际补充结果，才能维护上述总账。优先复用此接收/池机制；如需暴露内部接收处理入口，
只添加必要的内部接口供RM使用，避免复制整套接收代码。RX异常必须停止发新额度，
不能将错误CQE、控制接收或pool释放直接算成已成功补回的数据位置。

这套预算不替代实际CTP丢包/重复/RNR契约：模型约束软件合法发送和补回事件；底层若
可能额外重复投递或丢失控制，仍需C1决定补偿。无依据时不声称任意发送方规模安全。

### 10.4 验证、计数与完成边界

新增link_model.py为标准库可执行设计模型，不是provider/mock驱动或产品运行时：

    python3 contrib/rm_ctp/link_model.py

本轮10项测试通过，包括无反向业务EP回复、目标导入回滚与复用、非inline缓冲保留、
资源满时pending、活动请求失效/重复/实例切换、多个EP共用额度、提前请求按数量补回、
真实补回前不造额度、双向请求先于数据，以及12个固定种子各5000步随机调度和最终排空。
随机检查在所建模型内逐步核对总账，不是完整状态空间证明或真实网络乱序/丢包验证。
这些测试尚未调用URMA、实际C实现、PMIx或网络，不能据此勾A4/A6硬件相关条件。

后续实际C测试先覆盖上述规则，继续复用已通过的RM CPU harness。节点恢复后先旧8B，
再新候选隔离加载和小规模单向/双向/多EP/FC/关闭回归，之后再到PPN20。
诊断轮记录iface_get_address、ep_get_address、connect_to_ep、基于iface创建EP、
Jetty创建、目标导入，以及FC request/grant的次数与时机；UCP wireup独立统计。
provider调用次数不能当成网络报文数。每项计数都标清所属层和统计区间。

性能轮关闭这些诊断，对固定旧RM/新RM/RC、相同进程/拓扑/队列预算做重复对照，分别报
MPI初始化、未预热首次通信和稳定通信，连同Jetty/目标/队列总量、进程及设备资源记录。
首次成本若前移到初始化，也必须报告总成本；不能只挑首次时延下降来宣称建链全面更快。
最终是否比RC有优势由数据决定，不保证导师不会要求调整方案，也不预写性能结论。

参考取舍：采用UM的iface地址和目标管理、DC的独立FC回信/请求身份、brpc的双额度和
控制余量/缓冲生命周期。复用UCX池、pending和完成规则，不复制UM CREQ/CREP/软件重传，
也不引入brpc的TCP握手、按连接Jetty池或注册内存池。

A4现在有可实施的软件选择及模型证据；实际CTP必要保证、C接入及节点记录仍按依赖闭合。
清单保留A4/A5/A6/C1未完成；后续按本节实现和验证，不再要求用户先让导师逐项定方案。


## 11. A4实际C实现子步：当前旧地址路径的目标复用

本节优先说明本轮产品范围，第10节仍是下一版完整建链/流控的实施方向。
本子步起点524abdb，已测产品4b77878ce；用户应用/ARM64构建/双节点目标复用与正常、显式FC回归均通过。
验收记录见[r1/acceptance_a4_target_20261023.md](r1/acceptance_a4_target_20261023.md)。仅关闭本功能子步，A4/A5/A6不新增勾选。
本候选整合第10节及其模型，但不包含先前32字节iface地址发布候选。

### 11.1 当前已经写入C的内容

四个RM EP/iface文件实现iface所有的目标表。key比较完整128位EID、uasid、Jetty ID、
mode、policy、type、flag、CTP类型和token，使用UCX已有khash，不比较结构体padding。
普通公开UDMA的import_jetty_ex每次分配软件对象并发起导入命令，因此不能假定
URMA API已替上层合并这些对象；此结论不表示底层硬件TP必定逐次新建，节点provider
身份也未因此得到新的确认。参考的是UM的iface目标表思路，没有复制其部分EID比较
或导入失败后留下未完成表项的写法；不改原有UM/RC。

connect_to_ep先取得目标引用、再导入该EP独立的KA段，两项都成功后才发布
CONNECTED、dep_id和发送用指针。段导入失败只退本EP的引用，不能删除其他EP的目标。
数据发送仍读原有ep->tjetty，不增加每个SEND的hash查询。未实现内部控制发送EP、
新的FC请求格式/请求表、peer窗口、RX总账、CONNECT_TO_IFACE或减少wireup握手。

第10节中“保留到iface关闭”的完整方案需要远端实例身份，本子步尚无该身份，
故采用更保守的存续期：所有持有EP及其在途CQE都释放后立即unimport并删除表项。
重建EP重新导入，避免无引用旧句柄跨远端重建长期复用。KA段仍由每个EP独立管理，
不把两个段地址相同直接当成段对象共享。之后的48字节地址/实例方案再决定延长缓存。

取消导入失败时保留零引用隔离项，阻止新EP借用，iface停硬件后重试清理。
若provider在关闭时仍拒绝清理，只保留错误日志，不宣称该异常已恢复或资源绝不泄漏。
这不是网络错误隔离/重传机制，也不解决现有每EP窗口的任意规模共享RX预算问题。

RM原有EP地址字段、顺序和原生字节序均不变（40字节），打包/解包改用packed布局和
memcpy，覆盖UCP地址中的非对齐位置；不顺带改公共URMA地址helper或RC/UM。

### 11.2 本轮验收方法与证据范围

CPU执行实际RM C源码，mock仅替URMA调用：多个EP连接同一目标仅一次Jetty导入，
dep_id/KA段独立；一个EP失败或先销毁不影响其他EP；CQE未到不退最后引用；目标分配、
Jetty/段导入失败后可重试；完整地址/导入参数隔离；128目标扩容后引用稳定；引用溢出
拒绝新增；iface已失败拒绝新目标；unimport失败隔离并在关闭重试；实际RM iface清理
函数先删除共享Jetty，再清理在途EP。保留原70000完成、pending/flush与11项FC异常回归。
检查40字节地址逐字段、非对齐和canary。ASan/UBSan开启，LSan按原运行器默认关闭。

独立x86 make使用公开SDK头文件及编译占位库，只证明构建，不能当节点provider测试。
本机无autoreconf，复用旧生成configure/Makefile.in/config.h.in，构建系统输入未修改。
开发测试发现过新fixture误用KA描述符类型及遗漏async完整定义，修正后再跑；未改变
产品机制来绕过这些fixture问题。第10节Python模型结果不扩展成C流控/硬件验证结果。

新增target_reuse.c用于两个UCP worker之间四个并存EP的单向64B校验；不用MPI，
TCP仅做地址bootstrap和完成标记，业务走所选UCX TLS。其本地双进程TCP用例已验证
程序收发/关闭；随后实际RM/RC双节点专项均通过。accept.py --target-reuse在节点编译、部署
该用例，使用隔离库/模块、进程专用环境和有界超时。两端payload退出0且RM每iface
成功import=1、reuse>=3、release=1才通过目标复用门禁。debug计数不是物理报文/TP计数。

正常MPI 8B及1/2/4星形回归继续复用原runner；它们验证已有功能不回退，不能代替
同目标多EP用例证明缓存命中。该缓存的直接收益仅限多个逻辑EP共用同一远端Jetty时
减少重复导入句柄；常规一远端进程一个EP的场景可能没有命中收益。性能和总内存变化
仍需实测，不能把本轮导入次数下降说成MPI建链握手已经减少或一定比RC快。


2026-10-23验收补充：CASE=r1_shared_4b77878ce_DbsqE3，模块ed196bd6...；
accept_20261023_172443中两端RM四EP各成功import=1/reuse>=3/release=1，RC同载荷通过；
三轮8B与1/2/4多对端回归通过。a4_fc_window8_7s8b80yt中RM/RC双方FC=y、W8、TX8、
RX64及六个单向突发回归通过。8B样本RC22.54/2.29/2.40us、RM2.14/2.10/2.07us，
高样本保留，性能目标尚未验收。原始大日志留节点，本记录依据用户摘要及固定runner解释。
此结果证明同目标多EP的句柄复用及限定功能回归，不把它写成完整建链优化或硬件故障恢复。


## 历史：2026-10-25编解码起步检查点（已由第12节候选取代）

用户确认按依赖推进的开发顺序（见REQUIREMENTS文首），以AtomGit rm-a-connection/990398f为基线。
当前新增rm_urma_addr.h/c，固定48字节iface地址、32字节控制头、100字节请求体；
整数小端、EID原字节序，使用memcpy读写非对齐字段，输出只在全体验证成功后发布。
校验版本/模式/长度/实例/请求号及K*W+2K+2<=R，预算计算使用64位中间值。

该模块已列入URMA构建清单。现有RM EP/iface、公共层及RC/UM运行路径没有改动，
尚未连接此模块。后续必须接入地址发布、peer账户、活动请求匹配、独立控制及实际RX补充，
不得只启用CONNECT_TO_IFACE就交付。编解码正确不代表已拒绝所有迟到grant：仍需活动请求表。

验证为x86局部编译及ASan/UBSan的5组实际C测试；原共享发送/目标CPU回归通过。
测试目录复用既有生成头及源码核对相同的公共对象，RM三对象重新编译；不是全量configure/make、
ARM64构建或真实CTP交付证明。节点版本和已交付A基线保持不变。

## 12. 2026-10-25定案：有界iface建链候选与源码责任表

本节优先于第10节初稿和末尾旧检查点。实现基线为个人AtomGit
`rm-a-connection/990398f43e7b589b4030e45d69b48c1766f5eac3`，整树与历史GitHub5d相同。
当前为**完整软件候选，等待ARM64和节点验证**；不挪用4b/638的结果，不宣称C1故障契约已闭合。
A4的软件选择至此定案；相关平台契约未知项仍明示保留，清单不据本地测试提前打勾。

### 12.1 第1步收口：哪些直接复用，哪些是新设计

| 问题 | 源码依据及决定 | 不据此宣称 |
| --- | --- | --- |
| 发送完成与本地缓冲 | RC `update_tx_res_by_cr`→公共`txqp_completion_desc`；RM保留相同outstanding/CQE归属。新非inline控制用已有tx.mp描述符，直到CQE或停止Jetty后回收 | 成功CQE意味着MPI接收方已经消费数据 |
| 接收缓冲 | 公共`handle_am`允许上层以INPROGRESS持有描述符；补回只有post成功才扣available。新总账观察实际补回数，不把收到/释放/尝试投递当信用 | pool中有对象就意味着硬件接收位置已补充 |
| RC/UM可靠性 | RC主要交给下层；UM有PSN、ACK、发送窗口、超时重发与顺序管理。新RM继续使用RM+CTP SEND服务，不复制UM或brpc的整个协议 | 当前provider已被本轮证明覆盖所有丢失、重复、RNR恢复情况 |
| RM顺序配置 | 匹配公开UMDK50288核心将默认RM转为OI、RC转OL、UM转NO；SDK注释分别是initiator/low-layer/unreliable non-ordering | 仅凭枚举名证明设备故障或所有跨目标可见性 |
| 建链信息发布 | 固定HMPI52d943的init发布worker地址，add_procs/get_ep从modex获取并create；HUCX已有iface打包和connect-to-iface入口；UM可参考但仍有CREQ/CREP | 所有MPI EP都在首次Send才创建，或UCP不再需要wireup |
| 共享额度 | 当前每EP窗口不能作为多个发送方共享JFR的总预算；新模式每peer一个账户，所有本地EP共用；控制收发不查业务EP编号 | 任意规模/动态成员都能不经信用注册直接使用 |

本轮不新增软件数据ACK或网络重发定时器。FC grant表示可用接收位置，而非业务交付确认。
请求号/实例隔离防止软件信用重复使用，不能替代整个数据服务的可靠性与顺序保证。
控制post临时资源不足保留同一对象；未知终态失败保留可能DMA-owned的缓冲，停止新发送/授信。
底层成功完成/停止DMA边界沿既有公共API和已测AM完成路径使用，不另造一套缓冲生命周期。

C1未核实的当前SDK/固件故障语义继续归C1：尤其静默丢失、底层去重/重试范围和终态通知。
它们没有被本轮正常测试证实；若需要软件补偿，按已确认执行顺序在依赖该保证的验收前补齐。
本候选不声称故障恢复和动态重启支持，也不以公开头文件匹配代替provider二进制身份。
本轮没有需用户立即转问的泛化问题；后续只提出经RC/UM/公共层对照仍影响实现选择的差异。

### 12.2 确定的地址、身份、目标与时序

- `legacy_ep`默认保留40字节EP格式和CONNECT_TO_EP，用于回归和回滚。
- 显式`iface_v2`只上报CONNECT_TO_IFACE：iface地址48字节、EP地址长度0；不同时启用两种建链能力，也不静默回退。
- 新地址为小端固定布局：segment VA/len(各8)、Jetty/segment flags/token(各4)、version=2(1)、RM=1(1)、flags=1(2)、实际RX深度(4)、K/W(各2)、instance(8)。Device地址沿用公共EID/uasid格式。
- 控制体头32字节：version/type/length(1/1/2)、credits(4)、requester/responder/request ID(各8)。请求附EID[16]/uasid(4)/完整iface地址(48)，体共100字节；回复体32字节。含原3字节UB头为103/35字节。
- 普通AM不再带旧FC位；纯控制标记只解析新控制体，UB EP字段不参与回信路由。先检查长度/版本/实例，再检查活动请求及数量，失败不发布半初始化输出。
- peer归iface，持有完整远端地址、目标/segment pin、共享额度、活动请求和去重水位，保留至iface关闭。每EP只持本地完成/pending/flush及自己的target引用。
- peer按完整EID/uasid/Jetty/instance复用既有khash查找；链表只用于清理。相同物理目标尚被旧实例pin住时拒绝新实例，不能把动态重启当作新窗口悄悄接入。
- 一个iface使用一个内部软件控制EP完成归属，共用已有Jetty；不新增本地Jetty、注册池或每对端控制EP。非inline控制复用已注册tx.mp。
- UUID采用UCX既有生成器；64位请求号在本iface内单调递增，0不用，溢出报错。它们是防意外旧消息混淆，不是鉴权/数学不碰撞保证。

初始化：创建Jetty/JFR及接收准备→计算实际R与预算→生成稳定地址→既有UCP worker打包/HMPI modex。
创建：UCP已有device/iface地址→校验→peer/target/segment导入或复用→创建本地逻辑EP；不交换RM EP地址。
通信：普通AM按既有上层AM ID分派；低额度时发请求→接收端按真实补回/预算发grant→活动请求验证后恢复peer额度并唤醒其EP。
关闭：停止进展与Jetty→回收posted控制及退役EP→释放pending控制→peer的segment/target pin→JFR/池。
单EP销毁不重置peer额度；已经post的请求不因EP销毁而重新赠送窗口。

### 12.3 有界预算与推进

要求显式FC=y、已知静态通信集合上界K、W>0；K计入整个iface生命期的不同通信iface实例，
包括进出方向和可能选中RM的self连接。不是直接用“远端rank数”代替；未知/动态成员不属首版。
例如只有rm/um:aux、两节点共5rank时取K=5；40rank若每rank一个选定RM iface并含self，取K=40。

设C=2K+2，要求K*W+C<=R。R是构造后实际已post的接收数，不能用配置深度冒充。
K=5/W=8/R=64满足；40rank、W8/R512满足预算但尚未测试；384rank、W8/R4096也只是设计配置。
每peer每方向最多一个活动请求，不做软件控制重发。初始保留KW责任，其余可用数据预算为R-C-KW。
数据post成功才扣额度；request记录本次新消耗n；grant只加n，不重置为满窗。
低额度触发点复用配置中的FC hard-threshold比例，按远端W计算，最小为1；不新设独立阈值参数。
收到数据不释放预算；实际补回先结清控制欠账，再结清数据欠账，才可授予新额度。
只在真正未post的临时失败时回滚grant暂占；未知终态失败不能假定远端没有收到。

每次数据发送预留至少一个SQ和JFC位置供控制；控制工作在普通pending前有界处理。
额度耗尽的peer反调度，不能挡住另一个peer。控制队列或控制RX欠账未处理时event_arm返回BUSY。
保留正常数据批量RX补充；控制到达/等待或idle重试时才允许短批量。上层未释放全部缓冲时正确背压，不伪造信用。
公共层新增可选accounted poll，原RC/UM入口保留原批量/错误行为。部分/不明post失败不回收可能DMA-owned描述符，等待硬件停止。
另修复公共device地址中EID/u32/u16的非对齐解引用，保持原字节布局及数值转换。

### 12.4 实现位置与本地证据

| 文件/入口 | 职责 |
| --- | --- |
| rm_urma_addr.h/c | 48字节地址及控制体编解码、输入/预算校验 |
| rm_urma_link.h/c：ready/get_address/connect | 地址准备和发布、peer缓存/segment pin、基于iface创建EP |
| link：sent/receive/progress/account_rx/cleanup | 共享额度、活动请求、独立回信、实际RX账本及关闭 |
| ep：构造/AM/pending/ep_link_post/完成清理 | 接入新模式并复用原发送队列、池和CQE；保留旧模式 |
| iface：query/FC接收/progress/event_arm/flush/cleanup | 准确能力、控制进展、错误传播及资源顺序 |
| base：poll_rx_accounted及地址pack/unpack | 可选实际RX补回结果；必要的非对齐修复 |

从638参考子步逐段复用内部控制owner及其30项CPU测试，未把638的未验收树整体作为基线。
完整本地autogen/configure/make/install及实际C ASan/UBSan回归；新模式19项集成场景、
独立codec5组、原11FC异常和70000完成，以及控制owner30项均通过。
公开50288头文件及compile-only占位库只支持本地链接，不是provider/硬件验证。

### 12.5 节点验收与完成门槛

新脚本`r1/accept_iface.py`复用固定MPI launcher、既有maps/observer检查及原星形/单向burst源码。
隔离构建部署后检查两端模块、加载、新模式8B、同条件legacy/new/RC三轮8B、1/2/4 peers的双向和单向burst。
新模式诊断记录实际R/K/W、iface地址导出/创建、旧EP地址/连接调用为0、FC请求/回复post成功数。
这些是**传输API计数，不是网络报文数**；UCP wireup独立保留，不能据此宣称MPI首次零握手。
资源观察器必须确认目标模式TX8、maps正确和Jetty退出live=0。普通功能、CPU异常、真实网络故障证据分开。

所有失败保存，RC OPEN不能隐藏，少量时延样本不代替性能无回退验收；未测PPN20、扩展、分片、RMA和故障恢复。
第2步为完整软件候选，不是已完成真实节点验收；A4/A5/A6的最终勾选及正式AtomGit交付以明示门槛闭合为准。

### 12.6 2026-10-26节点结果与legacy地址回归修复

62fb在ARM64构建通过，用户返回的新模式三轮8B以及1/2/4 peers双向/单向共9例均通过；
1/2/4规模资源检查通过。旧d58 8B为RC2.23us/RM1.97us，候选新模式/RC三轮中位数为2.21/2.57us，
legacy三轮8B和多对端失败，部分RC用例发送完成status2；整轮未通过，不能作无回退结论。
节点CASE为r1_shared_62fb1bc63_lyxO2g，结果iface_accept_ap2fc5ar/results.json。
这些依据用户终端摘要，助手未下载原始节点结果；不补造逐轮样本。

legacy代表日志显示worker地址发布返回Unsupported operation。源码核验：
UCP address.c的地址打包在iface_addr_len=0时仍调用uct_iface_get_address；
原990 RM与当前RC为ucs_empty_function_return_success，UM则导出自己的非零长度地址。
62fb新回调错误地对所有非iface_v2返回UNSUPPORTED，这是本轮引入的兼容回归。
修复仅让legacy返回UCS_OK且不访问输出地址；未知模式仍拒绝，iface_v2编解码及流控不变。

新增回归通过公共uct_iface_get_address调用实际RM回调，覆盖零长度地址的NULL/非NULL输出、
缓冲canary及未知模式。测试先对未修复62fb实际对象失败，再对修复对象通过；
ASan/UBSan下原共享队列/目标/控制owner/新iface/RX/codec回归也通过。
本次修复待重新隔离构建及双节点复验；RC status2继续OPEN，不改RC源码，不宣称系统OOM或已证明平台根因。

### 12.7 2026-10-26修复后完整正常矩阵通过

产品2a8e3b771、ARM64模块26b9dac6在legacy/new/RC共28项矩阵全部通过；
三组8B原始值分别为2.26/2.27/2.27、2.20/2.20/2.28、2.54/2.56/2.55us。
7例新路径诊断：iface地址导出44、iface连接58、旧EP地址导出0、旧EP连接0，
FC request/grant成功post各114624；这些是不同规模汇总的API计数，不是网络包或首次握手数量。
资源门禁全部通过。完整证据、范围与原清单缺口见r1/acceptance_iface_20261026.md。

第1步的软件选择和第2步正常功能实现有通过证据，但完整A4/A5/A6暂不提前打勾。
上述直接UCT专项随后通过，见下节；下一小步进入A7反复创建/销毁及失败回滚；
CPU/节点/底层契约证据分开。性能、PPN20、C1故障语义仍未验收，原62fb失败历史保留。

### 12.8 2026-10-26直接UCT四EP专项通过

固定2a8e/26b9模块，地址发布/接收先于业务EP创建（48B iface、0B EP、R64/K5/W8）；发送端四业务EP，接收端零反向业务EP，64条×64B全部校验通过，short/bcopy各32条。FC request/grant各10，补回60额度。发送端目标import/reuse/release=1/14/1，接收端1/10/1，模块/maps均通过。

工具及证据见r1/acceptance_direct_20261026.md；本专项不经过MPI，不代替MPI初始化打点，也不证明UCP wireup消失。关闭上述节点专项缺口，转A7生命周期/异常验证；C1与A8边界保持。

### 12.9 A7生命周期候选：复用既有机制，产品不变

新增14个实际C测试及完整既有harness在ASan/UBSan下通过；补充两轮iface、32代业务EP、在途销毁与pending取消/重提的节点工具，待执行。源码审查及覆盖范围见r1/a7_lifecycle_20261026.md；不借此声称必要C1或A7完整验收已完成。
