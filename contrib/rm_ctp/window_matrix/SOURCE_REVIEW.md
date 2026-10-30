# 本轮源码审查与取舍

OSU固定7.5.2；复用此前从官方地址取得的源码包，未替换节点二进制。
官方归档：<https://mvapich.cse.ohio-state.edu/download/mvapich/osu-micro-benchmarks-7.5.2.tar.gz>
归档SHA256：`618de3d0b1122f73a9229177d2da1e5cd62e431190580cb915f2605849cbbbdc`。

| 原版文件 | SHA256 |
| --- | --- |
| `c/mpi/pt2pt/standard/osu_bw.c` | `89c79b05498470c3e080cbaaa43ae66b847767a2091c828706dd4426aa19d974` |
| `c/mpi/pt2pt/standard/osu_bibw.c` | `c76c9018a292eef82e525726545f822d19f8e295dbb765e05533f131a5f5ba58` |
| `c/mpi/pt2pt/standard/osu_multi_lat.c` | `41ed695d3ee149a0e580e340e3543a4d900a01f1c97f1298a861374d82bca38a` |
| `c/mpi/collective/blocking/osu_alltoall.c` | `b9aaa696402fe085c65e735d718b0c4c67dbbf100b4673854ff6068ac364ce2f` |
| `c/util/osu_util.c` | `ba579f62275aaed167613bb37454563d2c44da9a51b2c59240bdc947849f0da4` |
| `c/util/osu_util_options.h` | `c23da385d9e6d0c854a5888a9cba902f6c99455a923ffe40e0956f62ab182106` |

## 源码已证实

1. `osu_bw.c`和`osu_bibw.c`要求总rank数为2；窗口内非阻塞发送/接收后Waitall，`buffer-num multiple`按窗口分别分配缓冲，校验使用迭代号加窗口下标。
2. 消息长度循环是`size *= options.message_size_incr`，默认/本轮倍率2；它是同一次MPI任务递增扫描，不是交替序列。
3. `osu_util_options.h`提供message-size、iterations、warmup、validation、window-size和buffer-num；本工具仍读取实际安装程序帮助，防止编译选项与源码不一致。
4. 固定HUCX源码`4a151952ae070c096ec19d6ac7c843fc9b172b57`中，UCP `ucp_tag_eager_bcopy_multi`调用已有分段提交及pending处理。RM `uct_rm_urma_ep_am_bcopy`成功后把描述符挂到完成队列；资源不足返回`UCS_ERR_NO_RESOURCE`，提交失败归还描述符。RC也由UCP分段后调用其AM接口，UM具备不同的底层软件可靠性机制，本轮不移植它。
5. 本轮继续复用已核对的HMPI启动参数、UCP/UCT能力检查与URMA调用观察器；没有为了新增测试再改URMA/provider或HMPI源码。

## 本轮待节点验证

窗口32的多片消息能否在既有FC8、TX64及所配RX额度下持续正确完成；所有大小结果和真实rank取证必须齐全。源码可说明调用路径，但不能预判硬件结果。

单条MPI消息经过哪种UCP eager子路径需更细的路径证据；仅观察SEND最大长度不能证明每条消息采用FIRST/MIDDLE。因此本轮只声明原版OSU数据校验、应用生命周期、库来源及SEND长度观察，不增加网络丢包/顺序/重传保证。

严格大小交替没有现成原版OSU输入选项；本轮保留未覆盖。遵守标准OSU原则，不改测试算法或引入新自编demo作为正式验收。
