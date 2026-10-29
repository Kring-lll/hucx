# B方向工作目录（2026-10-29用户确认）

| 位置 | 当前路径 | 用途 |
| --- | --- | --- |
| 新电脑 | `C:\Users\y00993710\projects\hucx-b-segmentation-dev` | 用户手动应用、审查及提交B方向代码 |
| 中转电脑 | `D:\HUCX_RM_CTP\hucx-rm-b-segmentation-dev` | GitHub指定版本中转与节点包存放 |
| 节点B工作根 | `/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev` | B方向上传与后续隔离工作目录 |
| 节点B上传目录 | `/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/upload` | 用户手动上传节点ZIP |

本轮包的准确位置：

- 新电脑外包：`C:\Users\y00993710\projects\hucx-b-segmentation-dev\HUCX_B_eager_boundaries_20261029_bundle.zip`。
- 中转内包：`D:\HUCX_RM_CTP\hucx-rm-b-segmentation-dev\hucx_b_eager_boundaries_20261029.zip`。
- 节点内包：`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/upload/hucx_b_eager_boundaries_20261029.zip`。

## 历史已测安装与结果保持原址

已测产品CASE仍为
`/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_2a8e3b771_ps2tIz`。
本工具按固定CASE核对产品、库及既有依赖，并将工具/结果写到该CASE下。
因此从新上传目录启动后，结果仍位于旧CASE是当前实现的预期行为，不是加载了错误的产品。
没有执行产品安装、历史日志或OSU依赖的迁移；后续如调整输出根须单独改工具参数和核验，不全局替换旧路径。

## 开发流程

新电脑手动修改/学习/审查并将过程版本同步个人GitHub；中转按指定SHA取包；用户手动上传、root运行节点。
每轮隔离，不覆盖可用安装；正式验收以原版标准OSU为主。GitHub保存过程和回滚点，
个人AtomGit按A/B/C方向分支、完整验收工作包追加中文commit，不照搬全部过程历史；不强推、不操作公司主仓。
节点通过不直接证明Windows本地已提交或远端已同步。新开发前核对HEAD/工作区，重要结果同步清单和交接记录。
