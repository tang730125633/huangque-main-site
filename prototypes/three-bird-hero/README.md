# Three.js 液态玻璃飞鸟实验版

独立视觉原型，不替换 `liquid-bird-hero`、泽龙首页或正式主站。

- Three.js `0.185.1` 与 Anime.js `4.5.0`，通过固定版本 import map 加载。
- 飞鸟暂时使用已确认的液态玻璃概念图，等待图生视频序列帧替换。
- Three.js 只负责星粒、流光与鼠标视差；`UnrealBloomPass` 提供辉光后处理。
- Anime.js Three.js adapter 负责材质、灯光和 Bloom 的首屏入场时间线。
- 鼠标控制视差与朝向，整页滚动控制飞鸟位置、尺度和镜头关系。
- WebGL 不可用时回退到现有液态飞鸟静态图。

Three.js 为 MIT License：<https://github.com/mrdoob/three.js/blob/dev/LICENSE>

Anime.js 为 MIT License：<https://github.com/juliangarnier/anime/blob/master/LICENSE.md>
