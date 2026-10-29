# B 阶段单次能力与标准 OSU 尺寸边界工作包

固定产品为已测 `2a8e3b77`，不改 `src`、原版 OSU 7.5.2 二进制、节点安装或 C 重传。仅在 `node89` 由 `root` 使用，`node97` 通过受限 SSH 验证相同配置与库。

默认只输出计划，不连接节点。`--preflight` 检查两端产品/OSU/HMPI 哈希、候选模块、有效 `UCX_RM_URMA_SEG_SIZE=4K` 与目标 `udmac0d1e2:1` 的 `am_bcopy <= 4093`，不启动双节点基准作业；OSU `-h/-v` 本身可能触发单进程 MPI_Init。`--run` 完成相同预检后，调用原版 `osu_multi_lat` 和 `osu_alltoall` 的数据校验选项；默认两节点 PPN1、4，消息大小 8、117、118、4092、4093、4094、8192、16384 字节。对每个尺寸每个用例独立启动；首个 8B 同时跑 RC 作环境门禁，其余只跑新 RM。每个结果保存实际命令、配置、双端进程与库映射、CPU 绑定、数据校验输出、清理和完整日志。失败立即停止，既有通过项不删除。

所有 RM 用例固定 4K 传输段、`RNDV_THRESH=inf`、`ZCOPY_THRESH=inf`，沿用已测 iface 建链和辅助 UM；不打开尚未验收的 RMA 或 zcopy。8B 回归与 B 边界前后的正确性才是本轮判据，输出的 OSU 耗时仅是单轮附带值，不作为新性能结论。8192/16384 字节业务消息通过也**不**证明单个 CTP WR 支持 8KB：按 UCP 现有分段机制可以由多个 AM SEND 完成。本包没有实际逐 WR 抓包，不能据此勾选设备单 WR 极限、底层丢包保证或完整 B2/B3。

示例（在节点空闲窗口、两端一致产品安装后）：

```bash
python3 -u hucx_b_boundary_20261028.zip --preflight
python3 -u hucx_b_boundary_20261028.zip --run --ppn 1 --benchmarks osu_multi_lat --sizes 8 4092 4093 4094 8192 16384
```

`--run` 默认矩阵约 36 个独立 MPI 任务，占用两节点最多 8 rank；先执行上方 7 项小矩阵，再按空闲窗口扩大。每任务默认 120 秒上限，失败只清理本轮带 nonce 和路径的进程。看到 `RESULTS_JSON` 后把关键终端输出和失败日志的最早报错返回分析。原始资料及更高规模标准 OSU 基线保持原位。
