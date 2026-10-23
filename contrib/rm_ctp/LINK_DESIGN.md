# RM+CTP 建链：源码核验与最小设计初稿

日期：2026-10-23。对应 REQUIREMENTS.md 的 A4，并涉及 A5/A6/A7 与 C1。
状态：本轮完成源码路径核验和可审查方案；尚未修改产品、执行新测试或完成整个 A4。
本稿尚未定案 FC 控制状态、线格式及验证方案，A4、C1 继续保持 □。
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

**最小方案方向：本地 EP 保留完成、pending、flush 和窗口状态；接收侧不为普通
AM 强制创建成对业务 EP。FC 通过请求中明确的回信信息和一次性请求标识返回。**
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
| FC 请求标识 | 发送 iface 分配，每次请求唯一；本地表关联 EP | grant 先查表再访问 EP；销毁/取消移除标识，迟到/重复回复丢弃 |
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
| 发送方收 grant | 校验标识仍绑定活动 EP，恰好恢复一次窗口并调度 pending | 不把迟到/重复 grant 应用到复用 ID 的新 EP |
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

本轮产物是源码设计初稿与需求文档链接；没有新产品commit、性能结论、节点PASS或A4/C1完成勾选。
