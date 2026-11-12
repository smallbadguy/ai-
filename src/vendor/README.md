# 三维库（内嵌在回放页里）

- `three.min.js`：three.js 发行文件，MIT 许可，文件头保留了版权声明。
- `OrbitControls.js`：three.js 的视角拖动控制，MIT 许可。
- 许可全文见 `LICENSE-three.js.txt`。

`replay/build.py` 生成页面时，把这两个文件原样写进 HTML，所以页面不联网也能打开，3D 视图可以直接拖动转视角。
