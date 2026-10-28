# B1～B3的源码入口：只准备基线，不提前启用能力

产品固定为2a8e，A8/A9结果尚未取得。此文件是后续入口，不是B验收结果。

1. 单次UCT操作能力由`uct_rm_urma_iface_query()`和
   `src/uct/urma/base/urma_iface.c:uct_ub_iface_cap_query()`共同决定。
   `max_short`来自设备允许的inline长度减协议头，`max_bcopy`来自
   `seg_size`减最小AM头；具体数值必须由当前设备query取得，不能把示例值写死。
   iface_v2还包含自己的data envelope/额度处理，B1必须核对实际上报长度与真实可发送payload是否一致。
2. `uct_rm_urma_ep_am_short()`复用inline SGE；`uct_rm_urma_ep_am_bcopy()`
   复用现有mpool及pack callback，额度不足返回NO_RESOURCE，交由pending推进。
   这一层继续传一片AM，不另写一套应用大消息分包缓存。
3. UCP已有`src/ucp/tag/eager_snd.c:ucp_tag_eager_bcopy_multi()`，调用
   `ucp_do_am_bcopy_multi()`，已有FIRST/MIDDLE打包及接收端重组路径。
   后续B2/B3先沿这条路径覆盖阈值前后、非整片、长短交替、多对端和数据校验，
   核对每片真实长度及FC额度只计一次。先保证上报能力准确，再验证UCP选择与分段。
4. 当前RM query明确屏蔽AM_ZCOPY、PUT/GET、atomic、peer-check等未经验收能力；
   A89只测8/64/1024B并强制RNDV/ZCOPY阈值inf，不因测试需要重新打开复制自RC的能力。
   B4的READ/WRITE/rendezvous分别处理注册、rkey、完成和源缓冲生命周期。
5. C重传按导师安排延期。本轮正常通信通过不能推出丢包恢复、动态成员、重启重连、
   故障条件下的有序/去重保证。B的正常分段与C的故障重发仍分开记账。

下一次开发起点：A89节点报告和全部样本回收后，先确认A的有界结论，固定一个可回滚基线；
在同一产品基线上做B1能力/长度审计和必要最小修正，之后进入B2/B3正常多片验证。
