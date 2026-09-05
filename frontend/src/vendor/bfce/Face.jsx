import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { createFace } from './core.js'
import './face.css'

const APPROVED_EXPRESSIONS = new Set([
  'idle',
  'thinking',
  'happy',
  'joy',
  'sad',
  'annoyed',
  'angry',
  'worried',
  'scared',
  'surprised',
  'content',
  'curious',
])

const normalizeExpression = (expression) => (
  typeof expression === 'string' && APPROVED_EXPRESSIONS.has(expression)
    ? expression
    : 'idle'
)

// <Face expression="curious" size={160} />
//
// Grab a ref to drive it imperatively:
//   const face = useRef(null)
//   face.current.react('bounce')
//   face.current.look(-1, 0)
const Face = forwardRef(function Face(
  {
    size = 140,
    expression = 'idle',
    mouth = false,
    pupils = false,
    track = true,
    blink = true,
    idle = true,
    className = '',
    style,
  },
  ref,
) {
  const host = useRef(null)
  const face = useRef(null)
  const safeExpression = normalizeExpression(expression)

  // Anything the engine reads once at construction goes in this dep list, so a
  // change rebuilds the SVG. `expression` is *not* one of those — it animates.
  useEffect(() => {
    const instance = createFace(host.current, {
      expression: safeExpression,
      mouth,
      pupils,
      track,
      blink,
      idle,
    })
    face.current = instance
    return () => {
      instance.destroy()
      face.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mouth, pupils, track, blink, idle])

  useEffect(() => {
    face.current?.setExpression(safeExpression)
  }, [safeExpression])

  useImperativeHandle(ref, () => ({
    react: (name, options) => face.current?.react(name, options),
    setExpression: (name) => face.current?.setExpression(normalizeExpression(name)),
    look: (x, y, ms) => face.current?.look(x, y, ms),
  }), [])

  return (
    <div
      ref={host}
      className={`bwface-host ${className}`.trim()}
      style={{ width: size, height: size, ...style }}
    />
  )
})

export default Face
