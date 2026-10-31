# 原生READ定向路径候选

起点：GitHub `8e7d9e76bc129556221627101f9cb133a29675b4`；对应WRITE阶段已在个人AtomGit `rm-b-segmentation/ef97266c348d0392b3d8190ca15e62fe08dbe9a6`交付。
该阶段产品与节点118ab相同。原可用CASE及安装保留；本候选需要新隔离构建，尚无ARM或设备READ通过结论。

## 本轮实现

NATIVE_READ默认n，与默认n的NATIVE_WRITE独立。仅显式开启时发布GET_ZCOPY，单连续IOV、单次1～4096B且不超过有效MAX_GET_ZCOPY；不开放GET_BCOPY、PUT_SHORT/BCOPY、AM_ZCOPY或原子操作。
重写原RC派生GET_ZCOPY入口，复用现有注册/rkey导入、共享Jetty、EP完成队列及读完成处理：

- 本地目标和远端源区间做不溢出的检查，拒绝空句柄、多IOV/跨步数据等未支持参数；零长不发DMA。
- 每条READ提交前预留完成记录，即使没有用户回调仍跟踪读字节，避免只扣不还。
- READ只消耗共享发送资源及读字节额度，不扣AM接收信用；不足返回NO_RESOURCE，保持同EP FIFO。
- WR独立零初始化；提交失败归还完成记录、恢复fence，不扣发送/读额度、不推进序号。EAGAIN/ENOMEM可等待，其余错误返回IO_ERROR，不fatal。
- 正常完成复用共用GET完成回调，取消或错误完成复用既有purge归还资源。最后一个CQE处理后也更新读字节，避免等待下一条CQE才能恢复额度。
- native READ或WRITE启用时，无AM信用的pending仍可下一轮重试；保留RESCHED_GROUP以避免当前dispatch自旋。

调用者仍需保持本地注册缓冲和远端key至完成/flush；本候选不增加网络超时重传，不把CPU错误注入写成设备故障恢复保证。
产品仅修改RM ep.c、iface.c、iface.h三个文件；不改RC/UM/共用MD/UCP/HMPI或报文格式。

## 已执行的本地检查

完整x86构建及安装通过，SDK为编译链接桩，不能证明设备能力。真实RM源码的既有共享发送、流控、WRITE、A7生命周期及地址编解码回归通过。
READ实际C测试在ASan/UBSan下通过：默认关闭、数据和完整尾部guard、回调/无回调、取消、错误CQE、在途EP销毁、精确读额度、完成记录不足、源/目标边界、零长、提交/导入失败回滚、无AM信用pending、FIFO、两个EP完成归属。
原WRITE工具12项和新READ工具10项检查通过；节点C探针本地编译通过，默认计划不执行SSH/MPI。Windows仅静态审查，ARM与设备尚未执行。

## 节点入口与边界

新CASE内执行`python3 -u source/contrib/rm_ctp/native_read/accept_candidate.py CASE --run`。
实际命令由交付的03_build_accept.sh自动填入新CASE，不手动沿用旧模块SHA或旧结果放行。

1. READ-only模式：NATIVE_READ=y、WRITE=n；两个进程，6轮8/64/4096B各两轮，同一注册区复用，第二轮组地址偏移8/64/128B。TCP仅交换地址/key及同步标志，全部业务数据经UCT GET_ZCOPY。reader等待完成回调后逐字节校验目标和整个保护区，provider校验源数据及保护区未被改写；核验双端maps、注册和key释放。
2. 新库WRITE回归：READ=n、WRITE=y，重跑已验收6轮WRITE；不拿旧库结果当新产品证据。
3. 双开关模式：在既有28正常矩阵的实际命令中同时设置READ=y和WRITE=y，检查真实命令回执与产品身份；最高PPN4。该矩阵保持原有RNDV/ZCOPY阈值，不能当原生大消息协议验收。

任一步失败停止后续阶段，日志另存；只清理本轮launcher/SSH进程组，远端由本轮timeout收敛，不操作其他作业。
六轮probe沿用client80/server90秒及控制器超时边界；正常矩阵沿用每任务120秒。无高PPN或额外性能轮。
原生MPI RMA和正式单边OSU仍待接通：GET_ZCOPY不等于GET_BCOPY，当前普通RMA选路所需组合尚未全部开放；不虚报能力或修改OSU以取得数字。C重传延期、PPN192暂停。
