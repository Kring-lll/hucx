# B1源码审查检查点（未完成验收）

起点：个人AtomGit rm-a-connection/957264713cae5999e4e8e658e858cbec2d8459ea，已实际fetch核对。用户回传GitHub记录bad8c824428f265bf5af903d41648fe110c4ae8c。产品继续与2a8e相同。本文件是B1过程记录，不是新产品或节点通过结果。

## 工作目录与分支安排

助手独立checkout为b_segmentation_20261028，A目录与历史保留。新电脑仍hucx-a-connection-dev，暂不要求切换。准备完整可审查工作包后，新电脑拟建独立B开发工作树及GitHub rm-b-segmentation-dev；AtomGit B方向拟用rm-b-segmentation，从本次A交付连续推进，子需求验收后再正式推送。拟定分支尚未创建/推送，不混称已存在。中转缓存与节点upload继续复用，节点新候选仍隔离构建，不覆盖2a8e。

## 源码已确认

- RM、RC iface构造均use_min_seg=0，因此公共层seg_size取配置；UM use_min_seg=1，取MTU与配置较小值。不能直接把UM的MTU限制套到RM，也不能把MTU当MPI消息总长上限。
- RM/RC均调用uct_ub_iface_cap_query。AM max_bcopy为seg_size减sizeof(uct_ub_hdr_t)；max_short为有效max_inline减公共头，UCT short另含8字节调用方header。RM发送short用uct_ub_am_short_hdr_t，bcopy用公共mpool和pack callback，不因iface_v2额外增加一套数据envelope。旧B_NEXT中envelope措辞需继续核对，不作为已有事实。
- 公共max_inline取配置与设备max_jfs_inline_len较小值。具体节点有效长度必须取当前实际查询结果，不能直接写死4KiB或64KiB。
- RM query屏蔽AM_ZCOPY、PUT/GET及atomic等未验收能力；B1/B2不为跑测试一次性打开。
- UCP tag/eager_snd.c已有ucp_tag_eager_bcopy_multi，调用ucp_do_am_bcopy_multi，已有FIRST/MIDDLE打包入口；优先复用，仍需接收重组和完成/额度路径审查。

## 下一步

沿公共能力初始化、实际WR长度、RX分配、UCP每片payload及重组生命周期核对约束，明确是否需要产品最小修正；再准备固定版本的只读配置查询与标准OSU边界矩阵。源码存在分段入口不等于节点多片已验收。标准OSU工具须覆盖实际边界前后和非整片，不新增自编通信demo。C延期、192排查暂停；本轮没有新节点任务。

## 第二轮长度与接收核对

公共uct_ub_hdr_t为packed(am_id:uint8, ep_id:uint16)，大小3；short头再含uint64调用方头，大小11。有效inline为I时，UCT max_short上报I−3，包括调用方8字节头，API buffer长度因此最多I−11；不能误判两者相差8字节即是bug。seg_size为S时UCT bcopy载荷S−3，包含UCP协议头。

UCP eager_middle按msg_id查fragment hash，按offset放入缓冲；尚未匹配first的middle可暂存。该逻辑不是传输层通用去重或跨消息保序，重复片段如何影响剩余长度、消息匹配顺序仍需审查。导师保序与重发拆分要求已同步清单，未增加PSN产品代码。

## node89实际能力查询（用户回传）

已测安装ucx_info -d -t rm_urma退出0，日志位于CASE/b1_caps_gy9u8_tt/ucx_info.log。目标udmac0d1e2:1上报am_short<=125、am_bcopy<=8189、error handling:none；其他已显示设备相同。本次是继承shell配置的本机查询，尚不是固定OSU运行环境下的双端UCP协议选择证据。

结合源码推算有效inline128、seg_size8192；普通tag eager路径还应扣UCP协议头，不能将8189称MPI分片阈值。普通tag eager short承载8字节tag后理论用户载荷117；普通eager-only的tag头8字节，若UCP max_bcopy未另裁剪则单片用户载荷8181；64位下FIRST头24、MIDDLE头16，对应每片用户载荷8165/8173。以上有条件源码推算，不代表已测运行阈值；必须核对proto选择、max_seg_size、rndv/zcopy阈值和实际MPI调用类型后再设计标准OSU边界用例。error handling:none是能力上报，不等于运行失败，也不证明底层可靠性。

## 用户指出CTP 4KB限制后的纠正

8189来自HUCX公共默认SEG_SIZE=8192减3字节公共头；RM use_min_seg=0，query没有在此处按CTP专用限制裁剪。因此ucx_info结果仅证明软件上报能力，不是硬件实证；此前称“实际能力值”表述过强。暂停以8181为已支持边界的测试设计。
公开固定UMDK c1b643e26c9265b8e609f49603531a2131b76361的RELEASE-NOTES.ch.md及URMA User Guide列出bonding_dev_0的CTP多路径模式最大发送报文4KB；不能自动等同当前udmac0d1e2非聚合路径。需继续核对当前provider单次WR限制与分段契约。若该路径单次SEND限4096，必须使总WR含公共头不超过4096，不能仅把MPI数据设4096；具体UCP头进一步占用片内空间。尚无证据证明底层会替8192字节WR自动切片，也不能直接认定8KiB有效支持。

## 4KB、64KB及8189：进一步对照公开UDMA实现

公开UDMA内核`udma_main.c`在查询端口状态时将`active_mtu`设为`UBCORE_MTU_4096`，将端口`max_mtu`设为4096；同一驱动将`MAX_MSG_LEN`定义为`0x10000`，通过`dev_cap.max_msg_size`上报65536。这证明该源码区分链路MTU和通用设备消息上限，不能从4096 MTU推出所有SEND WR必须小于等于4096，也不能从65536通用设备能力推出CTP专用路径允许65536。

对应用户态UDMA provider的`udma_fill_send_sqe`在非inline分支把每段SGE长度填入发送WQE，已审阅路径未按4096裁剪或在软件中拆分该WQE；这不足以证明硬件会将更大的CTP SEND自动切为多个包。公开UMDK的4KB表明确针对聚合设备`bonding_dev_0`，当前已测HUCX设备名为`udmac0d1e2:1`，需要核对实际设备和配置版本后才能推广。

当前HUCX公共默认`SEG_SIZE=8192`，RM的`use_min_seg=0`，因此`ucx_info`报告UCT AM bcopy最大8189。实际`uct_rm_urma_ep_am_bcopy`将packed载荷长度加3字节UCT头填入**一个URMA SEND WR**，本层没有按4KB拆成多个WR。标准OSU的MPI消息大小又不是单WR大小：UCP可把MPI消息分成多个AM。这三个层次必须分别验证。

**B1临时执行边界：**先通过只读能力查询核实当前节点在设置`UCX_RM_URMA_SEG_SIZE=4096`后是否报告`am_bcopy <= 4093`；通过之前不得把旧8189当作CTP有效极限，也不得把4093当作MPI用户消息极限。若查询成立，后续从保守4KB UCT片长开始做原版OSU的分片边界校验，另行取证真实WR长度、UCP协议与两端配置；如必须支持超过4KB的单次CTP SEND，须有该设备/固件契约或实测证据。此项配置方案只是待验证的工作假设，本记录不勾选B1、B2或B3，也不改产品默认值。

## 2026-10-28只读查询失败及配置名纠正

用户在node89对已测安装分别设置`UCX_RM_URMA_URMA_SEG_SIZE=8192`和`=4096`，目标设备`udmac0d1e2:1`均报告`am_short <= 125`、`am_bcopy <= 8189`。这仅说明该错误变量未改变有效配置，**不证明**硬件支持8KB。仓库已有`r1/acceptance_20261022.md`记载相同的嵌套前缀误用：有效变量是`UCX_RM_URMA_TX_DEPTH`，重复`URMA_`曾被忽略。对应当前字段应为`UCX_RM_URMA_SEG_SIZE`；需先在同一进程`ucx_info -c`中核验配置值，再查询UCT能力。新命令尚待用户执行，不假称节点结果。

## 2026-10-28有效配置与本机能力已核验

用户在node89的固定已测2a8e安装上，分别设置正确的`UCX_RM_URMA_SEG_SIZE=8192/4096`，同一进程中`ucx_info -c`显示8K/4K；`ucx_info -d -t rm_urma`对`udmac0d1e2:1`分别报告`am_bcopy <= 8189/4093`。这证实配置生效及UCT软件上报为SEG_SIZE减3字节公共AM头。原命令并未在node97重复、未提交真实较大SEND WR、未核验UCP多片选择；4KB的安全性仍作为保守试验配置，设备/固件的硬件单WR限制不能由这组查询确定。

新工作包[boundary/README.md](boundary/README.md)按此配置进行双端能力预检，并以原版标准OSU校验4KB阈值前后和超出单片的大小；产品层没有新增分片协议或打开未验收原生能力。节点未执行前，B1/B2/B3保持待验，C仍延期。
