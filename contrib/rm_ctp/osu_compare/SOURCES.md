# 源码及实现取舍

## 当前固定基线

- GitHub开发起点：`1a3996a3f7ad260b8619972fae6a4446a6ad8631`，整树`90eec9d088deae543313bb5ab8911af5d48ed761`。
- 产品`src`等同节点已测`2a8e3b771211e5af491c8ca483ebcc01a03c2f01`；module SHA256=`26b9dac631af27622d0f04f3f86758332bb59874f4cf21743d2820c5783e3aa6`。
- AtomGit个人方向分支`rm-a-connection`最近确认`a817972d6390f798a962ba81561ba07b1a0dc15c`；本工具包不直接推送AtomGit。

## 官方OSU源码核对

2026-10-27从官方地址读取：

- <https://mvapich.cse.ohio-state.edu/benchmarks/>
- <https://mvapich.cse.ohio-state.edu/download/mvapich/osu-micro-benchmarks-7.5.2.tar.gz>
- 官方tar SHA256：`618de3d0b1122f73a9229177d2da1e5cd62e431190580cb915f2605849cbbbdc`。
- `c/mpi/pt2pt/standard/osu_multi_lat.c` SHA256：`41ed695d3ee149a0e580e340e3543a4d900a01f1c97f1298a861374d82bca38a`。
- `c/mpi/collective/blocking/osu_alltoall.c` SHA256：`b9aaa696402fe085c65e735d718b0c4c67dbbf100b4673854ff6068ac364ce2f`。

核对`osu_util_options.h`：两用例支持message-size、iterations、warmup、validation；alltoall支持full。
节点用户已确认两个-h正常，但筛选输出只显示-m示例，故工具自行保存完整帮助并核对所用选项。
上述官方源码仅用于设计核对，不替代节点已安装二进制的来源证明；本工具额外记录双端二进制hash/版本/依赖。
没有将第三方源文件或重新编译的OSU替换节点原版程序。

multi_lat在每轮MPI_Barrier后，对成对进程MPI_Send/Recv计时、平均并除以2；同步和统计可能建立额外连接。
alltoall计时仅包围MPI_Alltoall，之后统计各rank时间，full输出平均/最小/最大及迭代；启用validation会增加校验及同步操作。
因此先独立校验，正式计时和资源轮都不加validation，参数完全一致，仅资源轮增加观察器。
两用例MPI_Init与MPI_Finalize位于计时循环之外，身份库只在这些边界取证，绝不包装数据操作或MPI_Wtime。

## 复用已有实现

- 复用现有保存的HMPI启动命令解析、UCX环境、RX/TX/信用预算、绑核、库哈希与配置检查。
- 复用已有URMA资源观察器，按真实应用PID核对，不在timeout/脚本进程加载观察器。
- 复用A89单连接压缩传输和逐文件校验；只补充OSU的两阶段文件约定，旧A89默认约定不变。
- OSU独立包装器复用原执行逻辑；针对原版OSU命令行不含输出目录，提供nonce+准确目录环境变量+starttime清理。
- 不改变RM/RC/UM传输逻辑，不复制brpc协议，不增加C重传，不在本包重启A9或开启B的大消息路径。

正式比较控制HMPI集合组件配置；结果只适用于该配置。所有失败、负收益和资源观察开销均保留说明。
