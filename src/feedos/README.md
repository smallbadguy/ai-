# 3D 回放场景资源

本目录的场景、引擎、控件与样式基于公司内部仓库 `feed-mob/feedmob-agent-orchestrator` 的 `feedos_console` 分支，源提交为 `ae03c631abae1c3e5577cbb6130cad544b2ff059`。这里只记录代码来源，不代表该源代码采用 MIT 许可；请勿自行增加许可证声明。

- `views/scene3d.js`、`engine.js`、`ui.js`：空间场景、时间线和界面辅助代码。
- `replay-data.js`：将回放案例中的人员、事件和数据源转换成场景数据；设备类型和布局由活动内容决定。
- `bootstrap.js`：同步主页面的当前事件与时区。
- `css/`：场景样式。构建时移除外部字体导入，整个场景随 HTML 一起离线运行。

three.js 与 OrbitControls 是单独的第三方依赖，版本和许可证见 `../vendor/README.md` 与 `../vendor/LICENSE-three.js.txt`。页面不向外部服务发送案例数据。
