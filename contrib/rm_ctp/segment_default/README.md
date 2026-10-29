# RM默认4KB片长：已测正常范围

## 本轮改动

RM配置表的公共URMA子表默认串由空值改为`SEG_SIZE=4096`。采用已有UCX子表默认覆盖方式，
不增加传输协议、缓冲结构或重传代码；公共表仍为8192，RC/UM配置表不变。
该默认值取自已有4K配置的正常节点验证，不声称当前硬件所有CTP单次操作上限均为4KB。
显式`UCX_RM_URMA_SEG_SIZE`仍优先，较大值的解析成功不代表较大SEND已验证。

片长包含3字节公共UCT头，因此默认AM bcopy能力为4093；MPI用户载荷还需扣UCP头。
MPI消息可以大于4KB，继续由现有UCP eager分段重组。默认LINK_MODE、MAX_PEERS和未验收RMA能力不改。

## 已检查与待验范围

- 在x86开发环境使用现有URMA头文件和编译桩库完成隔离编译/安装；不代表真实URMA设备可运行。
- 对编译后的真实UCX配置解析器使用无设备桩库核验：默认RM=4K，RC/UM=8K；显式RM=2K/8K均能覆盖默认值。
- 新工具12项本地检查通过，包括上述3项真实配置解析；原WR/OSU回归分别16/23项通过。
- ARM隔离构建、双端默认值、28项既有正常回归与18项原版OSU边界均通过，已测产品80b28a25。
- 原版osu_multi_lat的PPN1 8B交错五轮已核对，新旧RM中位数均2.23μs；其余性能及硬件/故障范围未据此闭合。见[阶段验收](acceptance_20261029.md)。

## 节点流程

从GitHub指定候选commit打LF源码包，在新B实验根下调用原隔离构建脚本：

```bash
export HUCX_RM_CTP_EXPERIMENT_ROOT=/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev
```

构建脚本新增可选实验根参数，默认旧根兼容；每轮mktemp创建全新source/build/install，不覆盖已测2a8e。
编译并行度仍为8。`build_and_accept.sh`从本轮独立构建日志读取CASE，核对build receipt，不读取共享latest指针。构建输出CASE和模块SHA后，调用该CASE自己的源码脚本：

```bash
python3 "$case/source/contrib/rm_ctp/segment_default/run_candidate.py" "$case" --run
```

不带`--run`只输出计划。root/node89显式运行，至多两节点PPN4；C延期、192暂停。
远端仅创建新候选CASE；已存在但模块不匹配则停止，不覆盖旧安装。

## 验收顺序

1. 核对build receipt、候选源码manifest、产品模块、两端基础库及原版OSU/HMPI身份。
2. 双端不注入SEG_SIZE，核对RM/RC/UM有效默认值和目标设备RM AM bcopy能力。
3. 复用既有28项正常建链/流控/资源回归。该项是历史回归，不冒称新增标准OSU用例。
4. 两种原版OSU的PPN4矩阵：16项RM尺寸校验与两项RC 8B门禁；全部8个实际rank核验配置、映射和逐SEND长度。
   原WR工具虽保留显式4K入口，新候选驱动在调用边界移除所有片长环境覆盖并核验实际每轮配置，避免脚本替产品提供默认值。
5. 在独立结果目录运行原版`osu_multi_lat`的PPN1 8B对照：旧2a8e RM先数据校验，随后新RM/旧RM/RC交错各五轮计时。
   时序参数5000迭代/1000预热，不加载WR观察器。输出完整样本、范围、中位数、相对变化及相同口径的Finalize前RSS。

普通矩阵和WR日志单独保存，终端只打印摘要。任何数据、身份、长度或退出失败都停止，保留完整证据。
单MPI任务120秒，启动器另加20秒；只清理本轮nonce/目录匹配的进程。历史回归同样使用原有本轮进程清理。
五轮同规模8B对照不等于所有指标无回退：不自行设性能容忍线，负收益照实保留。本批五轮样本已核对，完整性能结论仍须覆盖剩余要求。
原有四EP/160次生命周期专项不在本轮重新运行，历史结果保留；若本候选触及其实际行为或出现新问题，再补相应定向复测。
硬件极限、丢失/重排/重复恢复、动态成员和RMA/rendezvous均不属于本次验收。
