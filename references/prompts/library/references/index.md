# 分类与检索导航

本包实际包含 **541 条原始案例记录**，逐字保留 prompt 字段，分为 13 类。其中 2 条注明原文未公开，只保留目标说明。案例编号不连续，最大编号不等于数量。

## 阅读顺序

1. 用下面的中文关键词确定分类；先读对应的「案例目录」而不是完整案例大文件。
2. 读该类的「模板与避坑」，挑选适合的结构。
3. 按目录中的行号读取 1–3 条完整案例；找不到时先搜索标题、风格、场景标签，不扫描全部正文。
4. 补充风格时读 [风格标签](<styles.md#styles>)、[场景标签](<styles.md#scenes>)，必要时再分段读取风格索引中的模板建议。

| 类别 | 关键词 | 条数 | 模板与避坑 | 案例目录 |
| --- | --- | --- | --- | --- |
| UI 与界面 | App、网页、仪表盘、社交截图 | 73 | [模板](<templates/ui.md#tpl-ui>) | [目录](<catalogs/ui.md>) |
| 图表与信息可视化 | 信息图、科普、知识卡片、流程图 | 53 | [模板](<templates/infographic.md#tpl-infographic>) | [目录](<catalogs/infographic.md>) |
| 海报与排版 | 海报、封面、字体、签名、排版 | 90 | [模板](<templates/poster.md#tpl-poster>) | [目录](<catalogs/poster.md>) |
| 商品与电商 | 商品主图、详情页、广告、包装 | 42 | [模板](<templates/product.md#tpl-product>) | [目录](<catalogs/product.md>) |
| 品牌与标志 | Logo、VI、品牌视觉、品牌系统 | 27 | [模板](<templates/brand.md#tpl-brand>) | [目录](<catalogs/brand.md>) |
| 建筑与空间 | 建筑、室内、空间、城市地图 | 12 | [模板](<templates/architecture.md#tpl-architecture>) | [目录](<catalogs/architecture.md>) |
| 摄影与写实 | 人像、胶片、纪实、商业摄影 | 78 | [模板](<templates/photo.md#tpl-photo>) | [目录](<catalogs/photo.md>) |
| 插画与艺术 | 水彩、绘画、艺术、装饰画 | 59 | [模板](<templates/illustration.md#tpl-illustration>) | [目录](<catalogs/illustration.md>) |
| 人物与角色 | 角色设定、姿势、卡牌、玩具 | 31 | [模板](<templates/character.md#tpl-character>) | [目录](<catalogs/character.md>) |
| 场景与叙事 | 分镜、故事、世界观、情绪场景 | 21 | [模板](<templates/scene.md#tpl-scene>) | [目录](<catalogs/scene.md>) |
| 历史与古风题材 | 长卷、古风、诗词、历史人物 | 16 | [模板](<templates/history.md#tpl-history>) | [目录](<catalogs/history.md>) |
| 文档与出版物 | 手册、报告页面、图鉴、出版排版 | 11 | [模板](<templates/document.md#tpl-document>) | [目录](<catalogs/document.md>) |
| 其他应用场景 | 研发拆解、爆炸图、特殊视觉实验 | 28 | [模板](<templates/other.md#tpl-other>) | [目录](<catalogs/other.md>) |

## 跨分类需求

- 电商海报：先商品与电商保证商品和卖点，再借海报模板约束文字与版式。
- 品牌产品照：品牌与标志确定视觉系统，摄影与写实确定镜头和灯光。
- 科普插画：信息图确定内容层级，插画与艺术确定笔触与材质。
- UI 图像只用于视觉参考，不等于可运行界面；数据图不得杜撰数值。

## 定位辅助

[manifest.json](<../manifest.json>) 记录每个案例的分类文件、行号和提示词 SHA-256。普通使用不必读它。
[完整模板全集](<templates.md>) 保留全部模板正文，分类文件是它的分段副本。
[docs 提示词总索引](<docs-prompts/index.md>) 收录 592 个来源位置及原文件副本，用于原文、完整来源和版本核对。
