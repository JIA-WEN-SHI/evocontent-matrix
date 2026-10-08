# EvoContent · 内容运营工作台

师嘉文 · AI 产品作品集项目

连接采集、草稿、人工审核与内容反馈。

**先查看：** [直接演示](https://jia-wen-shi.github.io/demos/evocontent/) · [项目案例](https://jia-wen-shi.github.io/#case-evocontent) · [作品集首页](https://jia-wen-shi.github.io/)

[观看 76 秒方案演示视频](https://jia-wen-shi.github.io/#case-evocontent/video)

无需登录 GitHub 即可浏览公开源码。案例页和公共原型不需要安装环境或填写模型密钥。

## 项目背景与职责

围绕内容运营组织采集、资料整理、选题、草稿、审核、人工发布登记和反馈。已有工程与迭代材料，完整的真实发布到反馈验证仍待补充。

我的职责：整体产品设计与实现。

## 当前范围

已有本地实现及近期安全验收记录。实际发布内容和完整反馈案例待补充，不宣称全自动运营成效。

公开演示可直接操作现有前端：模拟采集、预置草稿与人工审核 · 示例反馈和 SOP 分析 · 不执行真实发布。演示使用合成数据，不代表真实业务或实时模型效果。完整后端仍需本地服务与自己的配置。

## 源码结构

`apps/ · services/ · infra/ · tests/ · scripts/`

这是当前工作区源码的发布快照，未附带旧 Git 历史。真实密钥、数据库、浏览器会话、日志、客户原始金融材料和依赖缓存不在仓库内。

## 无后台演示

```bash
cd apps/web/demo
npm ci
npm run dev
npm run build
```

静态产物在 `apps/web/demo/dist/`。复用内容工作台已有示例流程；公开入口禁用网络请求，不连接平台账号，刷新恢复示例。

公开版打开时载入 8 条公共示例资料、8 条账号示例和 3 份待审核草稿。建议依次查看资料、编辑并审核草稿、查看 SOP 分析与人工确认建议；也可以重新模拟采集。互动指标与策略分析为预设，不能用来证明真实运营收益。演示视频记录的是本地方案版本，界面与最新公开版可能略有不同。

## 本地运行

需要 Node.js 24、Python 3.11+、Supabase/PostgreSQL 和自行配置的模型服务。

```bash
npm ci
# 将 .env.example 复制为 .env，填写自己的配置
pip install -e services/api
pip install -e services/agent
```

将 `infra/supabase/migrations/` 的迁移应用到自己的数据库。分别在三个终端运行：

```bash
# 在 services/api 目录
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 在 services/agent 目录
uvicorn app.main:app --host 127.0.0.1 --port 8100
# 在仓库根目录
npm run dev:web
```

网页默认为 3001。当前发布流程以人工发布、登记和反馈为主；历史自动发布代码不代表已验证的业务成效。浏览器登录状态需自行配置，不随仓库提供。

## 待补充的案例材料

- 谁提出需求，希望解决哪一项运营困难
- 一条从参考资料到草稿的完整实例
- 实际使用情况及发布、反馈验证边界
- 项目时间和当前最新版本说明

## 说明

这里展示产品探索与当前实现阶段，不宣称真实用户业务效果。商业素材和合作案例会在材料与展示范围确认后补充。
