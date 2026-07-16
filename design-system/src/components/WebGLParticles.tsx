import { useMemo, useRef, useState } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import * as THREE from 'three'
import { color } from '../tokens'
import { ParticleField } from './ParticleField'

function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas')
    return !!(window.WebGLRenderingContext && (c.getContext('webgl') || c.getContext('experimental-webgl')))
  } catch {
    return false
  }
}

function Points({ count = 1400, tint = color.gold }: { count?: number; tint?: string }) {
  const ref = useRef<THREE.Points>(null)
  const positions = useMemo(() => {
    const arr = new Float32Array(count * 3)
    for (let i = 0; i < count; i++) {
      // 球壳分布，带厚度
      const r = 3.2 + Math.random() * 2.6
      const th = Math.acos(2 * Math.random() - 1)
      const ph = Math.random() * Math.PI * 2
      arr[i * 3] = r * Math.sin(th) * Math.cos(ph)
      arr[i * 3 + 1] = r * Math.sin(th) * Math.sin(ph) * 0.6
      arr[i * 3 + 2] = r * Math.cos(th)
    }
    return arr
  }, [count])

  useFrame((_, dt) => {
    if (ref.current) {
      ref.current.rotation.y += dt * 0.08
      ref.current.rotation.x += dt * 0.02
    }
  })

  return (
    <points ref={ref}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial
        size={0.045}
        color={tint}
        transparent
        opacity={0.85}
        sizeAttenuation
        depthWrite={false}
        blending={THREE.AdditiveBlending}
      />
    </points>
  )
}

export interface WebGLParticlesProps {
  count?: number
  tint?: string
  className?: string
  style?: React.CSSProperties
}

/**
 * WebGL 3D 粒子球（Three.js / R3F）。金色粒子云缓慢自转，叠加混合发光。
 * 放在深色容器内 position:absolute inset:0 做背景，内容叠在上层。
 */
export function WebGLParticles({ count = 1400, tint = color.gold, className, style }: WebGLParticlesProps) {
  const [ok] = useState(() => typeof window !== 'undefined' && hasWebGL())
  if (!ok) {
    // 无 WebGL（无头浏览器/老设备）→ 退化为 canvas 粒子星网，绝不崩
    return <ParticleField count={90} tint={tint} interactive={0} className={className} style={style} />
  }
  return (
    <div className={className} style={{ position: 'absolute', inset: 0, ...style }} aria-hidden>
      <Canvas camera={{ position: [0, 0, 8.5], fov: 55 }} dpr={[1, 2]} gl={{ antialias: true, alpha: true }}>
        <Points count={count} tint={tint} />
      </Canvas>
    </div>
  )
}

export default WebGLParticles
