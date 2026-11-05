# 三模式首批正常SEND对照：节点已审计

证据为用户节点终端完整GROUP输出及只读断言，未下载原完整JSON。
CASE=/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-um-send-dev/r1_shared_a4d07599b_BaJAce。
结果CASE/um_osu_three_936frmcy/results.json，SHA256=dd6693098648d59db8ab4ac548be8efaddad84b4fbbbbce80c8dc087e1767a17。
产品a4d07599b、模块48dcd30a30f29a39d8f9b63e0c2afd7279bdd3da23b1757a610c828b77b04c2f不变。
用户新电脑实际已推GitHub工具2203631daddb9e3a4134dfdb91beea89f3089b05，AtomGit未追加本阶段正式交付。

MPI129/129、三门禁、18校验/90计时/18资源、6完整组、510个实际rank身份及目标模式Jetty释放复核通过。
有效片长RM4K、RC/UM8K，inline128，TX64/RX512；RM/UM窗口32、TIMEOUT10秒。SEND eager/bcopy，不能当所有生产默认值比较。

| OSU | PPN | 大小/单位 | RC中位 | RM中位 | UM中位 |
|---|---:|---|---:|---:|---:|
| multi_lat | 1 | 8B/us | 2.30 | 2.07 | 2.16 |
| multi_lat | 4 | 8B/us | 2.31 | 2.06 | 2.15 |
| alltoall | 1 | 8B/us | 2.39 | 2.15 | 2.21 |
| alltoall | 4 | 8B/us | 4.15 | 4.71 | 4.59 |
| bw | 1 | 64KiB/MB/s | 6624.82 | 7323.27 | 7162.37 |
| bibw | 1 | 64KiB/MB/s | 11465.08 | 8187.96 | 8101.62 |

Alltoall PPN4五轮RC=[4.07,4.13,4.18,4.15,4.63]us，RM=[12.01,4.55,4.71,4.54,8.45]us，UM=[4.60,4.53,4.55,4.59,4.64]us。
RM中位4.71比RC慢13.49%、比UM慢2.61%；RM均值6.852，RC均值4.232、UM4.582，不能用中位数隐藏两次高值。
原始全样本已保留；高值未归因为环境、调度或重传，正常计时没有重传探针。

计时轮RSS合计中位MiB（两节点各rank Finalize前合计、非同步）：multi_lat PPN1 RC120.273/RM55.738/UM55.758；
multi_lat PPN4 RC518.043/RM229.992/UM227.660；alltoall PPN4 RC521.949/RM229.684/UM230.824。
每rank目标模式Jetty峰值相应RC2/5/8、RM1、UM1；RC辅助UM不混计。RSS不能全归因于Jetty或解释成设备独占内存。

本批RM点对点8B比RC低约10%～10.82%，单向64KiB高10.54%，双向合计却低28.58%；RM与UM中位接近。
完整C/D、丢包恢复性能、严格交替、规模/硬件故障/READ_WRITE未验收。后续30/12轮复测单独统计，不合并覆盖此批。
