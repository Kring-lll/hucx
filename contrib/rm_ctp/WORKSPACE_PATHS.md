# B方向工作目录（2026-10-29用户确认）

| 位置 | 当前路径 | 用途 |
| --- | --- | --- |
| 新电脑 | `C:\Users\y00993710\projects\hucx-b-segmentation-dev` | 用户手动应用、审查及提交B方向代码 |
| 中转Git打包仓库 | `D:\HUCX_RM_CTP\hucx-b-segmentation-packaging-git` | 已创建独立Git仓库，按固定SHA/tree归档 |
| 中转文件存放目录 | `D:\HUCX_RM_CTP\hucx-rm-b-segmentation-dev` | 早期工具包存放；该目录不是Git仓库 |
| 节点B工作根 | `/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev` | B方向上传与后续隔离工作目录 |
| 节点B上传目录 | `/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/upload` | 用户手动上传节点源码包或工具ZIP |

历史边界工具包位置：

- 新电脑外包：`C:\Users\y00993710\projects\hucx-b-segmentation-dev\HUCX_B_eager_boundaries_20261029_bundle.zip`。
- 中转内包：`D:\HUCX_RM_CTP\hucx-rm-b-segmentation-dev\hucx_b_eager_boundaries_20261029.zip`。
- 节点内包：`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/upload/hucx_b_eager_boundaries_20261029.zip`。

## 当前默认4KB产品与历史依赖

当前B已测CASE：`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_80b28a25b_dvKqlu`。
源码commit `80b28a25b5ea0e245bca88db44be79d15d06127f`；模块SHA256 `8d9ed4133c71c4ca77ad900e60433e9781cfef2adf33dc396f3f642b2cc9821c`。
本阶段验收输出为该CASE下`segment_default_accept_94i91scl`，不再将旧CASE称为当前唯一安装。
源码包位于新B upload下`hucx-b-default-80b28a25b-source.tar.gz`，中转原包在独立Git打包仓库目录。

旧2a8e CASE、原版OSU、HMPI及资源观察器仍保留在历史实验根
`/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019`。
旧CASE为`r1_shared_2a8e3b771_ps2tIz`。旧边界工具在新upload启动却写到旧CASE，是当时固定安装的预期行为。
当前默认片长驱动明确绑定新CASE；历史依赖路径不搬迁、不全局替换。B实验根无需Git初始化。

## 开发流程

新电脑手动修改/学习/审查并将过程版本同步个人GitHub；中转按指定SHA取包；用户手动上传、root运行节点。
每轮隔离，不覆盖可用安装；正式验收以原版标准OSU为主。GitHub保存过程和回滚点，
个人AtomGit按A/B/C方向分支、完整验收工作包追加中文commit，不照搬全部过程历史；不强推、不操作公司主仓。
节点通过不直接证明Windows本地已提交或远端已同步。新开发前核对HEAD/工作区，重要结果同步清单和交接记录。
