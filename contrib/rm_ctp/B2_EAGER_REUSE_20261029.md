# B2/B3：复用现有UCP分段重组，补齐真实头部边界

本文记录旧2a8e上显式4KB的边界审查与验证。后续80b28a25已将4KB固化为RM默认并再次通过同范围测试，见[阶段验收](segment_default/acceptance_20261029.md)；既有UCP分段重组代码未改。

源码起点：GitHub `rm-b-segmentation-dev/4a61e88ddf8884f61893d5e0da1baf9c45e8d250`。
产品仍为节点已测 `2a8e3b771`，本工作包不改 `src`。C重传延期，PPN192暂停。

## 已有证据与这一步的目的

PPN4原版标准OSU两用例共18项正常尺寸校验已通过。随后逐SEND观察器在PPN1的
8/4093/4094/8192/16384B校验均通过；总SEND长度最大分别为103/4096/4096/4096/4096B。
节点目录为CASE下 `b_wr_probe__f91fgym`。依据用户回传终端记录，未下载完整节点JSON。
这些结果说明本轮4K配置下大于单片的业务消息可正常交付，不能当作底层硬件最大报长保证。

原先以4093为界是UCT载荷边界，尚未精确夹住UCP用户载荷边界。本步补齐4085及两片容量附近的
正常路径标准OSU验证，并把逐WR长度证据扩展到PPN4的全部8个实际OSU进程。

## 分层实现已存在，最小改动是复用

| 层次 | 源码及函数 | 当前责任 |
| --- | --- | --- |
| UCT能力 | `src/uct/urma/base/urma_iface.c`的公共能力查询，RM iface query | 配置段长减3字节公共头；RM继续屏蔽未经验收的AM_ZCOPY/RMA等能力 |
| UCP发送 | `src/ucp/tag/eager_snd.c`的`ucp_tag_pack_eager_first_dt`、`ucp_tag_pack_eager_middle_dt`、`ucp_tag_eager_bcopy_multi` | 首片带tag、总长和消息ID；续片带消息ID和偏移；按AM bcopy能力决定本片长度 |
| UCP资源不足 | `src/ucp/proto/proto_am.inl`的`ucp_do_am_bcopy_multi` | 提交失败恢复打包前datatype状态；NO_RESOURCE交给既有pending机制，避免把未提交片误记已发送 |
| RM发送 | `src/uct/urma/rm_urma/rm_urma_ep.c`的`uct_rm_urma_ep_am_bcopy` | 复用mpool、pack callback与发送额度；每个AM加公共头后提交SEND，不在这里再复制一套消息分段协议 |
| UCP接收 | `src/ucp/tag/eager_rcv.c`的`ucp_eager_first_handler`、`ucp_eager_middle_handler` | 消息ID查fragment hash；按偏移放入用户缓冲；未匹配首片的续片可暂存 |
| UCP完成 | `src/ucp/core/ucp_request.inl`的`ucp_request_process_recv_data`及tag fragment队列处理 | 解包后减少remaining，到齐才完成请求并移除相应匹配状态 |

RC与UM也经由同一UCP tag分段重组；差异在UCT能力、资源和传输语义。
UM另有传输层PSN/确认/重试，UCP的消息ID和偏移不能代替那层功能。
新协议框架`tag/eager_multi.c`同样有FIRST/MIDDLE和`egr/multi/bcopy`，但当前固定测试明确
`UCX_PROTO_ENABLE=n`，因此这一步以旧框架`eager_snd.c`调用链为依据，不把新框架代码当作运行取证。

## 当前配置下的边界计算

固定ARM64、普通tag eager、SEG_SIZE=4096、公共UCT头3B：

| 项目 | 长度 | 说明 |
| --- | ---: | --- |
| UCT max_bcopy | 4093 | 包括UCP头，非MPI纯载荷 |
| eager-only头 | 8 | tag，因此普通single用户载荷上限推算4085 |
| eager-first头 | 24 | tag8 + total_len8 + msg_id8，首片载荷推算4069 |
| eager-middle头 | 16 | msg_id8 + offset8，续片载荷推算4077 |
| 首片加一个续片 | 8146 | 4069+4077，单AM lane且未另裁剪时的两片容量推算 |

**以上是条件化源码计算，不是每一种MPI操作实际协议选择的证明。** 同步发送头、集合算法、
UCP阈值或有效lane上限会改变实际选择。本轮已测试8/4084/4085/4086/8145/8146/8147/16384B，
两种标准OSU、PPN4。每项100校验迭代、10预热；另有两个RC 8B回归门禁，共18个独立MPI任务。
18项均已在节点通过，见[节点记录](wr_probe/WR_BOUNDARY_NODE_20261029.md)。本轮不测性能，不将观察器耗时填入性能表。

## 实现和验收边界

复用已在节点通过的C观察器，不改符号转发或WR内容，只扩展Python编排的尺寸、用例和PPN。
两端固定产品/OSU/HMPI、实际应用PID、nonce、Init后和Finalize前库映射逐rank核验；
peer摘要单次SSH取回，保留原文、SHA256和完整结果。任意数据/退出/身份/长度失败立即停止。
单任务rank超时120秒，沿用本轮nonce/目录/进程起始时间限定清理，无全局pkill或设备重置。

通过只表示上述正常路径和长度边界可用。总WR含控制/集合消息，不能用总数除以业务消息数
证明片数；post成功也不是成功CQE或远端交付，后者由独立OSU数据校验证明本轮业务结果。
观察器不跟踪Jetty分配/释放，进程退出及无标记残留不等于Jetty live=0证据。
不测试丢失/重复/重排恢复，不自动关闭C1/C4，也不宣称B1设备契约、B2/B3全范围或B4已完成。
本步不增加64KB/MB消息、RMA、rendezvous或另一套PSN协议。
