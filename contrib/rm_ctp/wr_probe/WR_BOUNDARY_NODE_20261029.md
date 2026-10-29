# PPN4精确分片边界与逐SEND：节点通过

依据：用户实际回传终端，未由助手直接读取节点完整JSON。
节点包SHA256=`f87e15d4478b872308c09c3b7d3b98edf439d1deb00dfdcb38b88573c3e50bcc`，本轮节点校验OK。
上传路径见[工作目录](../WORKSPACE_PATHS.md)。产品仍为已测2a8e，没有重编译或修改transport。

结果目录：
`/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz/b_wr_probe_wqis04cj`。

## 已通过范围

- 双端局部加载符号转发模拟门禁通过。
- 原版标准OSU 7.5.2，两节点PPN4共8个rank，SEG_SIZE=4096。
- RC 8B回归：`osu_multi_lat`、`osu_alltoall`均DATA=PASS、EXIT=0。
- 下列RM每项均DATA=PASS、EXIT=0、RANKS=8/8、LENGTH_BOUND_4096=PASS，共16项。

| 业务消息字节 | multi_lat观测SEND最大字节 | Alltoall观测SEND最大字节 |
| ---: | ---: | ---: |
| 8 | 103 | 103 |
| 4084 | 4095 | 4095 |
| 4085 | 4096 | 4096 |
| 4086 | 4096 | 4096 |
| 8145 | 4096 | 4096 |
| 8146 | 4096 | 4096 |
| 8147 | 4096 | 4096 |
| 16384 | 4096 | 4096 |

最终“WR诊断=通过”。本次18项全部通过，为B2/B3正常路径的边界和PPN4多对端覆盖补证。
业务消息超出单片仍完成数据校验，观测的RM SEND均不超过4096B。

## 结论边界

4084+11=4095、4085+11=4096与公共头3B加普通tag头8B的源码计算一致，
但最大长度摘要本身不是逐消息FIRST/MIDDLE头解码，不能单凭最大值认定每条消息准确片数。
总WR含流控/建链/集合通信；post成功不等于发送CQE或远端交付契约。
本轮无性能结论，不验证硬件最大报长、网络分包、丢失/重排/重复恢复或Jetty无泄漏。
B1底层契约和B2/B3未覆盖范围不自动全部勾选；RMA/rendezvous未开启，C延期、192暂停。
