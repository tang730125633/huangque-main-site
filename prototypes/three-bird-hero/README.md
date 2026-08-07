# Three.js 液态玻璃飞鸟实验版

独立视觉原型，不替换 `liquid-bird-hero`、泽龙首页或正式主站。

- Three.js `0.185.1` 通过固定版本 import map 加载。
- 飞鸟使用 MiniMax H3 生成的 5 秒静音循环视频；透明 WebM 为主、MP4 与概念图为兼容回退。
- Three.js 只负责星粒、流光与鼠标视差；`UnrealBloomPass` 提供辉光后处理。
- 鼠标控制视差与朝向，整页滚动控制飞鸟位置、尺度和镜头关系。
- WebGL 不可用时回退到现有液态飞鸟静态图。

Three.js 为 MIT License：<https://github.com/mrdoob/three.js/blob/dev/LICENSE>
