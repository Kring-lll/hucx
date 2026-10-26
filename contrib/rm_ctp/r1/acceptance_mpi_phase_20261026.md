# 2026-10-26 MPI地址阶段证据通过

本专项补齐A5的MPI运行阶段证据，不修改产品、HMPI或UCP，不作为性能/网络包测试。当前GitHub记录基线22060611a825e677116394b06322f67105517959，产品仍2a8e3b771211e5af491c8ca483ebcc01a03c2f01；ARM64模块SHA256为26b9dac631af27622d0f04f3f86758332bb59874f4cf21743d2820c5783e3aa6。

## 身份与复现

CASE：`/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz`。
结果：`a5_mpi_phase_0sot496v/results.json`。已执行zipapp SHA256：`17be3ed1e6152cc5f378aea0e568431ca2d7660ee8f5a500260164c75b1d31b9`，节点校验OK。

仓库入口a5_mpi_phase_20261026.py及a5_mpi_phase.c；前者仅将本地C文件名调整为仓库文件名，压缩包成员mpi_phase.c及运行逻辑不变。C字节与已执行zipapp一致。复用a7_iface_four_ep_20261026.py的SSH/身份/maps工具；复用已固定accept.py/accept_iface.py的保存launcher及配置，节点只编译/部署小测试binary。

证据来源为用户原始终端及固定工具门禁，完整节点JSON/日志未下载。两轮8B双向内容校验、两端候选maps和新RM tag lane门禁通过。

## 实际阶段

| 两个rank均相同 | MPI_Init内 | 首次数据通信 | 第二轮数据通信 |
| --- | ---: | ---: | ---: |
| HMPI worker地址打包/发布日志 | 2 | 0 | 0 |
| RM iface地址导出API | 2 | 0 | 0 |
| 获取对端worker地址 | 0 | 1 | 0 |
| HMPI发起连接/实际RM iface连接 | 0 | 1 | 0 |

旧EP地址和connect_to_ep回调为0。地址长度/实例/预算字段的直接证据沿用同产品此前UCT专项，实际R64/K5/W8；本次MPI运行阶段与其共同构成A5证据。

结论是“初始化时已经发布地址，本次HMPI首次业务通信时获取并创建逻辑连接，后续复用”，不是“初始化结束所有远端EP均已创建”。每rank导出2次是API/worker地址打包计数，不等于两个Jetty、按每EP交换或网络重发次数。HMPI源码存在分别打包remote/local范围的分支，精确scope标签以节点原日志为准，不由本汇总臆造。

固定HMPI52d943的mca_pml_ucx_init调用send_worker_address；add_procs/get_ep按配置可能提前或延迟创建。本节点结果证明本次走首次通信获取/建链，不把它推广为所有HMPI版本的行为。PML Pack日志在不同编译分支中的位置不同；不当作modex网络传输完成时间戳。不宣称UCP wireup消失或首次通信零网络握手。

## 剩余必要契约与源码对照

A5地址阶段证据与A7软件注入/正常节点回归已齐，不重跑。A4软件方案和A6正常实现已通过，正式联合交付前的必要C1未知项限定为当前RM+CTP SEND的数据服务保证：发送成功完成/最终失败与远端交付、同一目标的顺序、去重/重试及接收位置不足处理。

已再次对照：RC的deal_with_send_cr用下层成功/失败CQE回收及通知，无UM式软件PSN窗口；UM有PSN、ACK/NACK、发送窗口和重发；所查看brpc UrmaEndpoint::SendAck/HandleCompletion路径用于归还接收信用和回收SQ，不能把其credit ACK当作本环境重传保证。匹配公开URMA头文件将RM注释为Reliable message，提供rnr_retry/err_timeout及ACK_TIMEOUT/RNR_RETRY_EXCEEDED错误码，但这些定义本身不证明当前CTP驱动/固件覆盖哪些条件。urma_post_jetty_send_wr的返回值只说明提交接口返回，不得把它当成成功SEND CQE或远端交付。

因此不再让用户查priority/TP/RPM/头文件哈希，也不增加硬件故障注入。只需平台确认：当前环境同一目标SEND是否由下层保证不丢不重、按序交付，送不到会报失败完成；接收位置暂时不足属于下层重试/错误通知还是可能静默丢失。若没有这些保证，数据或独立FC可能静默丢失，当前无网络补发定时器就可能等待；是否增加补偿将据明确契约决定，不猜测、不预先复制UM协议。

完整C1故障恢复、A8性能/PPN20均未验收；本次仍只归档现有个人GitHub分支，暂不进行AtomGit正式联合提交。
