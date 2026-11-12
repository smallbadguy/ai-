# 页面字体（内嵌在回放页里）

和原版 FeedOS 控制台用同样的字体：

- `lato-400.woff2`、`lato-700.woff2`、`lato-900.woff2`：Lato（界面文字），拉丁字符子集，许可见 `OFL-lato.txt`。
- `jetbrains-mono.woff2`：JetBrains Mono（数字、时间、编号），可变字重，拉丁字符子集，许可见 `OFL-jetbrainsmono.txt`。

两种字体都是 SIL Open Font License 1.1，文件取自 Google Fonts。`replay/build.py` 生成页面时把它们转成 base64 写进 HTML，
所以页面不联网、打不开 Google 的网络下也能正常显示。中文字符这两种字体里没有，浏览器会自动用系统中文字体。
