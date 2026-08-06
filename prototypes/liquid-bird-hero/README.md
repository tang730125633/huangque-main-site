# 液态光影飞鸟首页样机

本目录只用于确认黄雀首页 B 方向的首屏视觉与交互，不是生产页面。

- `index.html`：首屏文案与语义结构
- `style.css`：液态玻璃飞鸟构图、响应式与静态降级
- `experience.js`：原生 WebGL 光场、鼠标视差与滚动起飞
- `public/assets/glass-bird.webp`：黄雀自生成视觉资产，不含第三方素材

指针光痕参考 React Bits `BlobCursor` 的多级阻尼与 `LightRays` 的鼠标影响思路，使用当前原生 WebGL/CSS 重新实现，没有引入 React、GSAP 或 OGL。
