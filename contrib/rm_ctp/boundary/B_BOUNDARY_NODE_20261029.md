# B 阶段标准 OSU 消息尺寸边界：节点正常路径记录

## 固定身份与范围

- GitHub 开发分支 `rm-b-segmentation-dev`，测试工具起点 `7b285d494f66978e874f7ebaea9c7c84eff7d2aa`。节点运行的包名 `hucx_b_boundary_20261028.zip`；该包的已交付 SHA256 为 `b48fa3890a4b8d2712679efc0303061ae05efb6974ca28a6be51c92fbd44b01c`。本次用户回传的终端片段未包含独立包哈希复核输出。
- 已测产品安装：`r1_shared_2a8e3b771_ps2tIz`，本轮未改变 transport 源码。节点输出显示“双端配置=4K AM_BCOPY=4093 产品/OSU/HMPI哈希=PASS”。
- node89 手动运行原版 OSU 7.5.2 工具包：`python3 -u .../upload/hucx_b_boundary_20261028.zip --run --ppn 4`。两节点每节点 4 rank，总计 8 rank。
- 原始结果：`/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz/b_boundary_uyym1lfs/results.json`。本记录依据用户回传终端输出；此处未直接读取节点 JSON，不补造内部字段。

## 已通过的正常路径

| 标准 OSU 用例 | RC 8B | RM 已验证的业务消息尺寸（字节） | 节点结果 |
| --- | --- | --- | --- |
| `osu_multi_lat` | DATA=PASS，EXIT=0 | 8、117、118、4092、4093、4094、8192、16384 | 每个尺寸 DATA=PASS，EXIT=0 |
| `osu_alltoall` | DATA=PASS，EXIT=0 | 8、117、118、4092、4093、4094、8192、16384 | 每个尺寸 DATA=PASS，EXIT=0 |

两种用例共 18 条通过记录。跨越 4K 片长上报边界后的业务消息仍能收发且校验通过，说明这组标准 OSU 正常通信路径在 PPN4 可用。历史 A 阶段回归与资源、性能结论继续按原证据记录，本轮不重新声明其全部通过。

## 未覆盖与下一步

`AM_BCOPY=4093` 是 UCT 对上层报告的能力，不是实际单条 CTP SEND WR 的长度证明。OSU 业务消息 8192/16384 字节通过，不能推断每片 WR 数量、底层硬件单次上限、按序保证或丢失后的恢复。B1 的底层单次限制仍待逐 WR 或接口契约核实；B2/B3 只能先记“上述尺寸正常功能通过”，不能记完整多片内部机制验收。C 重传按导师意见延期，RMA/rendezvous 未在本轮开启。

下一步在不改变产品的前提下核对既有发送路径的片长/提交 WR 证据，再决定是否需要补充定向取证；若节点时间允许，再按既有工具覆盖 PPN1 的同一边界与大小交替。任何失败和波动均保留，不把正常路径通过推及故障可靠性。
