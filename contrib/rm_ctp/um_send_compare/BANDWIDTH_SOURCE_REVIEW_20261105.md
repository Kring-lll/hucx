# 单向／双向带宽与复测口径

原版OSU7.5.2来自官方归档 https://mvapich.cse.ohio-state.edu/download/mvapich/osu-micro-benchmarks-7.5.2.tar.gz ，归档SHA256=618de3d0b1122f73a9229177d2da1e5cd62e431190580cb915f2605849cbbbdc。
未修改节点OSU二进制。已读c/mpi/pt2pt/standard/osu_bw.c与osu_bibw.c：

| 源文件 | SHA256 |
|---|---|
| osu_bw.c | 89c79b05498470c3e080cbaaa43ae66b847767a2091c828706dd4426aa19d974 |
| osu_bibw.c | c76c9018a292eef82e525726545f822d19f8e295dbb765e05533f131a5f5ba58 |

## 已确认的基准程序行为

osu_bw.c 233..248：rank0每窗口发window_size个MPI_Isend、Waitall，再Recv一个1B确认；rank1 301..324：Irecv/Waitall后回MPI_Send 1B。
364..367最终分子为size×iterations×window_size/1e6，除以rank0测得的总秒数。主要大数据是0→1，反向仍有OSU小确认及底层控制。
OSU的1B确认是MPI层基准消息，不等同RM/UM传输层的PSN累计ACK。

osu_bibw.c 236..264、328..363：两端都先挂Irecv，再Isend；rank0等待send和recv，rank1等待recv和send，双方同时收发。
410..414分子额外乘2：2×size×iterations×window_size/1e6。因此输出是双向合计有效应用数据MB/s，不是每个方向独立带宽。
把bibw输出除2只能作对称负载下的直观均摊，不是实际测得的两个方向各自吞吐。
计时分母包含软件处理、等待、确认和底层资源影响；不是网卡标称线速。单向快不保证双向合计恰为2倍。

## 产品源码支持的假设与边界

固定a4d中RM/UM使用软件窗口、ACK/NACK、发送保留与重发，收发由worker progress驱动。双向使每端同时承担业务发送、接收、ACK及完成回收，可能暴露共享CPU/队列/内存瓶颈。
RM同步iface_progress先处理RX，没有RX进展时poll TX；异步路径另会poll TX。RC也有RX优先、条件poll TX逻辑，不能仅看到RM这个分支就归因。
RM poll_tx一次取1个CQE；RC按tx_max_poll批量取CQE；一个CQE可能覆盖多个WR，不可按1与批量数字直接推算性能。
软件ACK/窗口与RC的完成/流控路径不同，RM4KB与RC/UM8KB片长也影响分片数。它们是可验证假设，不是28.58%差距的已定位根因。
没有在正常计时轮观察重发，不能把抖动直接称为重传。

前批单向RC/RM/UM=6624.82/7323.27/7162.37 MB/s；双向合计=11465.08/8187.96/8101.62。
bibw/(2×bw)约86.5%/55.9%/56.6%，仅描述本批单/双向扩展比例，不等于硬件链路利用率。
RM与UM相近提示共同的软件路径值得研究，不能证明已排除环境、硬件或具体实现问题。

## 新复测

三模式各30次PPN4 8B alltoall，各12次PPN1 64KiB bw和bibw；每轮旋转顺序，次数为3倍数以平衡先后位置。
每模式/负载另1校验+1资源轮，外加三模式8B门禁，共183任务；最高PPN4。保持原迭代/预热/窗口/CPU绑定，产品和OSU不改。
新旧批次分别统计，不合并、筛掉高值或重跑替换失败。分位数是独立OSU整轮平均值的跨轮分位数，不是逐消息尾延迟。
--repeat-study包含--timing；不带--run仅显示计划。现有默认六组模式保持兼容。

负载快照只在启动器前后读取/proc/stat、loadavg、meminfo、pressure、softirqs及进程comm/CPU ticks/affinity，不采集完整cmdline，不启动计时区间内采样守护进程。
记录整次MPI启动至退出附近的负载；可能漏记两快照间退出的进程、短突发或卸载URMA流量。
同CPU affinity只表示可能竞争，不证明实际同时抢核；列表也可能包括控制器、SSH和守护进程，不能自动判定是其他人的作业。
负载采集失败单独记error，不改写成功的OSU结果，也不声称环境干净；PID重用按start_ticks检查。
其他作业即使不占同一核，也可能争用LLC、NUMA内存带宽、IRQ/softirq或链路。最好在协商的低干扰窗口运行，保留共享环境结果。
本包不停止其他作业、不改CPU/IRQ/sysctl或设备配置，不把复测完成当完整C/D、硬件故障或READ/WRITE验收。
