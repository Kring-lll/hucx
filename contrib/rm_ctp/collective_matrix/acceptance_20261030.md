# 原版OSU集合大消息正常阶段验收

日期：2026-10-30。对应B5所选正常场景子项；完整B5及B未收口。

## 固定版本与证据

- 产品：`c7ef02b51d274e4ff3151457b82cc34f493419d5`；模块SHA256：`cc12a6ed9299f87f36f927b9d4f8f8043810ec3273a96a55734a4cc89b2848f2`。
- 工具：GitHub `740184d8db0555f186bb08988b335c77e21fb884`，产品未改，复用已测c7ef安装。
- CASE：`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_c7ef02b51_vkSZ69`。
- 预检：`b_collective_fptb45_0/results.json`；正式矩阵：`b_collective_du17qffs/results.json`。
- 证据来自用户实际终端及只读JSON断言。原始完整报告和日志留在节点，未下载；[结构化摘要](acceptance_summary_20261030.json)不是原始报告替代品。

## 通过范围

| 原版OSU7.5.2 | 模式 | PPN | 每rank业务尺寸 | 任务数 | 尺寸行数 |
| --- | --- | ---: | --- | ---: | ---: |
| alltoall、allgather | RC/RM | 1 | 8B门禁 | 4 | 4 |
| alltoall、allgather | RM | 4 | 8B～64KiB，乘2递增 | 2 | 28 |
| alltoall、allgather | RM | 4 | 16385B非整片 | 2 | 2 |
| alltoall、allgather | RM | 4 | 64KiB，阈值inf配置对照 | 2 | 2 |

10/10任务、36/36尺寸行校验通过；全部任务退出0，数据、候选库maps及绑定门禁通过。
20迭代/2预热，两个节点最多各4个进程。双端默认RM4096、RC/UM8192，未用SEG_SIZE环境变量覆盖。
原生RMA与AM_ZCOPY能力保持关闭。

只读复核确认：56/56实际进程独立日志完整，8个RM任务的52/52份实际PID发送观察完整，SEND最大长度均不超过4096B。
RM两项8B门禁最大SEND为103B，六项PPN4任务最大为4096B；RC两项不作SEND观察。
post成功不等于远端交付，本轮数据正确性依赖原版OSU独立校验；没有混用wrapper或ucx_info的PID代替应用证据。

## 结论与限制

在c7ef已测产品上，所列Alltoall和Allgather正常场景可用，包含多rank、跨片尺寸和更大业务消息。
本轮只新增contrib自动化，未改传输产品、HMPI或原版OSU通信算法，未重新编译产品。

- Alltoall尺寸是每目标块，Allgather是每rank贡献；集合内部可聚合/拆分，不把业务尺寸当单SEND长或UCP请求长度。
- 阈值和AM scheme已核对，但不是集合内部实际请求路径的逐条证明。
- 本轮校验与观察器开启，不作带宽/时延优势或全部性能无回退结论；未新增Jetty观察，不宣称完整资源泄漏验收。
- 递增扫描与固定尺寸不等于反复大小交替，该项仍待补。
- 原生READ/WRITE、注册/rkey生命周期、完整B以及C故障恢复未验收；C延期，PPN192暂停。

需求清单只勾选B5的集合正常子项，B5整体保留□。此前AM阶段五轮8B/RSS结论不外推为本轮集合性能。
本阶段代码与记录拟在现有AtomGit `rm-b-segmentation` 追加一笔中文提交，不新建子需求分支；推送状态以实际用户回执为准。
