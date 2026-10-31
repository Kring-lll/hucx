# 基础原生RMA与普通UCP读写阶段验收

已测产品：`989fd71865046bde988603c97416dc133c582e88`，ARM隔离构建通过。
模块SHA256：`7b08a804a02d67fdc965b0055a55459ebf0dc4af198cbe4096ccf91019fb2530`。

证据来自用户终端输出，以及用户在节点读取results.json、子结果和实际启动命令记录后执行断言的回传；本记录不是节点原始JSON的拷贝，未直接下载节点全部日志。[结构化摘要](acceptance_20261031.json)保存准确路径和范围。

## 通过范围

- 新库原生UCT READ六轮与WRITE六轮，各含8/64/4096B重复验证；数据、候选库maps和注册释放均通过，READ另核对远端源与完整guard。
- 基础UCT八轮：PUT_SHORT、PUT_BCOPY、GET_BCOPY，含偏移与完整guard。
- 普通UCP两组，各16轮：PUT八轮、GET八轮，按8/65536/64/16385/4096/8193/8/65536B严格交替；数据、guard、双端候选maps、实际原生路径与退出通过。
- NATIVE_RMA=y后，既有28项正常矩阵全部通过，实际命令记录已逐项核对该开关。

实际成功post计数如下；这是传输入口计数，不是网络报文、MPI单条消息片数或重传次数。

| 用例 | PUT_SHORT | PUT_BCOPY | PUT_ZCOPY | GET_BCOPY | GET_ZCOPY |
| --- | ---: | ---: | ---: | ---: | ---: |
| 基础UCT | 2 | 2 | 0 | 4 | 0 |
| UCP BCOPY组 | 5 | 39 | 0 | 44 | 0 |
| UCP ZCOPY组 | 5 | 0 | 39 | 5 | 39 |

ZCOPY组中部分小片仍走SHORT/BCOPY，按现有UCP阈值选择；不把配置名称当成每个片段都走同一路径的证明。

## 修复与回归

旧b189在64KiB GET时校验失败，记录保留于旧CASE。实际旧C源码CPU复现flush越过pending尾部，修复后断言开关ASan/UBSan测试通过，再由本轮新库完成真实节点复验。
产品修复只给RM ep_flush增加pending顺序检查，确保未提交分片先完成排队推进，再等待最后CQE及unpack，不加延时、不放松数据校验；见[定位记录](flush_fix_20261031.md)。

## 边界

NATIVE_READ、NATIVE_WRITE和NATIVE_RMA仍默认关闭；单次SHORT遵守inline/4096较小限制，BCOPY遵守片长/4096较小限制，ZCOPY保持单连续IOV和1～4096B等既有上限。64KiB由现有UCP分段，不等于单次URMA支持64KiB。

此轮证明所列双节点正常UCT/UCP原生读写，以及开启新能力后的正常回归。28项里的MPI双边小消息不等于MPI单边操作；MPI_Win、正式单边OSU、原子依赖、多对端原生RMA矩阵、更完整的注册/rkey复用及完整B4仍待验。历史AM/高规模结果不自动成为本产品重验结果。
本轮未作性能/资源对照，不使用功能诊断数据宣称优势或全场景无回退。C延期、PPN192暂停不变。此次仅整理记录与交付，不再修改产品，不要求节点重跑。

## 原始结果位置

```
/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_989fd7186_xncBHX/native_rma_accept_7ssm55bv/results.json
/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_989fd7186_xncBHX/native_read__1mgoiq4/results.json
/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_989fd7186_xncBHX/native_write_6rm7pe1s/results.json
```
