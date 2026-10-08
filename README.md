# EvoContent · 内容运营工作台

师嘉文 · AI 产品作品集项目

连接采集、草稿、人工审核与内容反馈。

**先查看：** [项目案例](https://jia-wen-shi.github.io/#case-evocontent) · [作品集首页](https://jia-wen-shi.github.io/)

无需登录 GitHub 即可浏览公开源码。案例页和公共原型不需要安装环境或填写模型密钥。

## 项目背景与职责

围绕内容运营组织采集、资料整理、选题、草稿、审核、人工发布登记和反馈。已有工程与迭代材料，完整的真实发布到反馈验证仍待补充。

我的职责：整体产品设计与实现。

## 当前范围

已有本地实现及近期安全验收记录。实际发布内容和完整反馈案例待补充，不宣称全自动运营成效。

公共入口为流程与界面的案例展示，未公开运行依赖密钥、数据库或本机服务的完整后端。

## 源码结构

`apps/ · services/ · infra/ · tests/ · scripts/`

这是当前工作区源码的发布快照，未附带旧 Git 历史。真实密钥、数据库、浏览器会话、日志、客户原始金融材料和依赖缓存不在仓库内。

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
