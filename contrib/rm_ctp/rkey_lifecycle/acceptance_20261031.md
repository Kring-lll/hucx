# 远端内存key前置修复验收记录

日期：2026-10-31。结论：共用MD四项修复的CPU检查和既有正常通信回归通过，可按本范围交付。
原生READ/WRITE仍关闭，本记录不代表设备RMA、真实远端key导入或完整B已经验收。

## 版本与改动

- 已测产品：`ac4146054b0e1364d0ea691b37b9f14238a43bb0`。
- ARM模块SHA256：`c211385e1fa4a5c4096f49cdc74b69af8ec82104e80edb9b460e0b7667789308`。
- CASE：`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_ac4146054_sbFyVf`。
- 最后续跑工具：GitHub `bb1a4cc37666f749c13e511d8e06271259eafe66`；产品与ac414一致。
- 相对上一笔AtomGit B交付，产品仅共用`urma_md.c/.h`两文件，+23/-15，RM/RC/UM均使用。
- 修复完整64位远端地址、完整16字节EID比较、分配失败回滚、未导入key的释放；packed rkey仍44字节。

## 正常回归与续跑

| 范围 | 结果 |
| --- | --- |
| 既有正常矩阵 | 28/28通过 |
| 片长边界 | 18/18通过 |
| 窗口与递增扫描 | 10/10通过 |
| eager及AM rendezvous | 10/10通过，单对至1MiB |
| 集合通信 | 10/10通过，最高PPN4、每rank业务至64KiB |
| 8B计时 | 三种模式各五轮，共15轮通过 |

这是多个检查点组成的同一产品回归，不是一次从头连续新跑。原始记录及工具失败保持不改：
`rkey_accept__txtvu2a`先通过28正常，随后控制器缺controller_dir中断；
`rkey_resume_j6c51b_5`完成边界、计时、窗口和前7项AM，1MiB任务退出0且数据Pass，因发送日志142.02MiB超旧128MiB门禁中断；
`rkey_log_review_g8q_c8y3`补验原1MiB的DATA/MAPS/WR/AM_PATH/FULL_LOGS/EXIT通过，随后对端父目录遗漏中断；
`rkey_log_review_08p4r89m`复用成功1MiB补验，仅新跑余下2项AM和10项集合，全部通过。
以上三次中断是测试工具问题，不能计为新增传输功能。流式日志仍核对实际进程、完整哈希、错误与大小；目录修复不覆盖既有结果。

最终报告：`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_ac4146054_sbFyVf/rkey_log_review_08p4r89m/results.json`。
用户随后只读复核产品/模块身份、最终状态、1MiB、余下2+10任务退出0且无cleanup_error、15轮原样本一致，断言全部通过。
本目录JSON是用户终端及断言输出的转录摘要；原始完整JSON/日志仍在节点，未把摘要伪装成已下载原件。

## 原版OSU 8B五轮

| 模式 | 五轮时延/μs | 中位数/μs | 最小～最大/μs |
| --- | --- | --- | --- |
| 旧RM（c7ef） | 2.23、2.22、2.22、2.23、2.25 | 2.23 | 2.22～2.25 |
| 新RM（ac414） | 2.22、2.22、2.22、2.20、2.24 | 2.22 | 2.20～2.24 |
| 同批RC | 2.51、2.52、2.51、2.51、2.52 | 2.51 | 2.51～2.52 |

基准为原版OSU7.5.2 `osu_multi_lat`，两节点PPN1，业务8B。旧RM中位数2.23μs、新RM2.22μs；新RM相对旧RM降低约0.45%，相对RC降低约11.55%。
保留全部样本及波动，不把该差异归因于本次rkey修复，不作所有场景无回退结论。
本轮未回收完整RSS原始样本，不填写内存收益；诊断开启的大消息时延/带宽不采用为性能结论。

## 验收边界与下一步

CPU五项直接覆盖真实MD源码，URMA为mock；完整x86构建为SDK链接桩，ARM构建与正常节点结果另列。
AM正常回归不能代替真实设备rkey导入、WRITE/READ数据及注册回收验证；并发import、context归属、重注册与unimport失败等仍按后续路径审查。
严格反复大小交替、原生RMA、完整B/C未验收。C延期，PPN192定位暂停。
下一项产品开发为单一原生WRITE：共享发送额度与pending、注册/导入、提交与错误返回、完成/flush、远端数据和释放；之后单独实现READ。
