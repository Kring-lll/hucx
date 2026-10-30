# B4 原生RMA前置：远端内存key的身份与释放

起点：GitHub `82cff2375b58768fa02c28ce961cef5d86676158`。
本工作包只修复共用MD代码的四项问题，不开放PUT、GET、AM_ZCOPY，不修改建链地址或报文格式。
产品修改仅为`src/uct/urma/base/urma_md.c`和`urma_md.h`；RC、UM、RM共用，必须回归RC及现有RM路径。

| 问题 | 原行为 | 修复 |
| --- | --- | --- |
| 64位远端地址 | 缓存key的va为32位，高32位被截断，不同内存段可能共用同一个key对象 | 内部缓存保留完整64位VA |
| 完整EID | 相等比较只看EID最后32位 | 比较全部16字节；哈希也混入全部EID和VA |
| 分配失败 | kh_put已插入条目，随后calloc失败没有撤销 | 删除本次新条目，允许重试 |
| 解包后未使用就释放 | 尚未import_tseg时返回NO_RESOURCE，留下ref_cnt=0的条目 | 无导入句柄则跳过unimport，仍删除并释放缓存对象 |

哈希碰撞本身允许存在，真正错误是旧相等比较把不同身份当成相同身份。
64位VA变化是内部缓存布局变化；对外packed rkey仍为44字节，不改变已有rkey交换格式。
URMA unimport失败仍保留原有错误处理，不释放仍可能被设备持有的对象；本轮不宣称该异常恢复已解决。

## 源码审查结论与后续顺序

1. RC和RM的PUT/GET入口已经存在，均经过共用rkey解包和延迟import_seg。复制来的入口不等于RM+CTP已可用。
2. RM接口能力屏蔽由R1阶段加入，是分阶段验收边界，不是硬件不能READ/WRITE的证明。
3. SDK的`urma_write`、`urma_read`最终提交WRITE/READ opcode，但通用接口存在不能证明当前节点的RM+CTP provider支持它们。
4. UCP wireup会依据PUT/GET能力选择rendezvous和RMA lane。因此仅删除mask会同时改变MPI大消息路径，不能这样一次开放。
5. WRITE下一步需核对：RM+CTP可用操作、注册和远端key、共享发送额度、pending顺序、提交失败返回、完成和flush、销毁时的缓冲/目标寿命。先实现一个操作并单独验收，之后才恢复GET。
6. READ还需处理读取额度及返回完成、零长路径、目标和本地缓冲注册；不能沿用AM信用作为READ可用资源的证明。
7. 已观察到的共用延迟import路径还需审查同锁内重复检查、本地context归属、相同地址/身份重新注册时的key一致性、import/unimport失败处理。本补丁没有宣称这些边界均已解决。

参考位置：`base/urma_md.c`的`uct_ub_rkey_unpack/release`；`base/urma_ep.h`的`uct_ub_ep_get_tseg/import_seg`；
`rm_urma_ep.c`与`rc_urma_ep.c`的PUT/GET；`src/ucp/wireup/select.c`的RMA带宽选择条件。
已核对的URMA参考源码为UMDK `50288b2a4a62fb76e3584acd8d8ed1c1071a485d`中的`urma_cp_api.c`、`urma_dp_api.c`及所存UDMA Jetty/JFR文件；
该有限源码范围不能替代当前驱动/固件能力确认。HMPI继续使用原有UCP路径，不新增MPI交换协议。

## CPU验证

`bash contrib/rm_ctp/rkey_lifecycle/run_cpu.sh BUILD_DIR URMA_INCLUDE`

测试包含真实`urma_md.c`，URMA仅为mock，使用ASan/UBSan；五项为unused/address/eid/allocation/shared。
旧版四项断言失败，shared成功；修复后五项通过。没有设备、网络或真实性能结论。
完整x86构建、新构建上的五项key测试、既有RM实际C/地址编解码回归通过；既有Python工具38项及新控制器3项通过。
旧版失败详情及实际检查项记录在`cpu_checks_20261030.json`。
LSan在受限运行环境默认关闭；用缓存条目归零及unimport次数验证本轮对象生命周期。

## 节点执行边界

必须从新源码归档隔离构建，不覆盖c7ef可用CASE。不再使用写死旧CASE的独立ZIP运行新库。
构建成功后运行新CASE内的`source/contrib/rm_ctp/rkey_lifecycle/run_candidate.py CASE --run`。
默认不加`--run`只列计划，不进行SSH/MPI。

运行器复用原版OSU、nonce限定清理、实际进程库加载检查、双端部署与完整日志：
28正常、18边界、10窗口、10 AM rendezvous、10集合任务，并与c7ef和RC各五轮8B对照。
最高PPN4；AM日志按实际host/PID隔离。任何失败立即停止，保留JSON及全部原始记录。
不修改OSU，不更改SEG环境默认，不将诊断时延当性能结论；五轮性能仍须审阅。
这些是修复后的现有功能回归，不能代替真实设备rkey导入/READ/WRITE数据及生命周期验收。
C继续延期，严格大小交替、原生RMA、完整B仍未完成；通过CPU测试不在AtomGit提前标记完成。
